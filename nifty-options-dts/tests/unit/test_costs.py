"""Cost model tests. Expected figures are worked out by hand from the NSE circular rates and
Zerodha's published tariff, typed here as literals so they check the data files independently."""
import copy
import datetime
import unittest
from decimal import Decimal as Dec

from nifty_core.costs import brokers, engine as eng, statutory
from nifty_core.costs.models import BUY, SELL, LegExecution
from nifty_core.errors import ContractError, CostRateUnverified, NiftyError
from nifty_core.versioned import load_json
from tests._base import NoNetworkTestCase

D = datetime.date
REAL = "real_readonly"
TODAY = D(2026, 11, 4)


def leg(side, price, units=65, day=TODAY, leg_id="L"):
    return LegExecution(leg_id, side, Dec(price), units, day)


class PerLegCostTests(NoNetworkTestCase):
    def setUp(self):
        super(PerLegCostTests, self).setUp()
        self.e = eng.load_cost_engine("zerodha", REAL)

    def test_buy_leg_by_hand(self):
        # premium 100 x 65 units = turnover 6,500
        # brokerage 20.00 | STT 0 (buy) | exchange 6500*3552.99/1e7 = 2.3094 -> 2.31 | IPFT 6500*0.01/1e7 -> 0.00
        # SEBI 6500*10/1e7 = 0.0065 -> 0.01 | stamp 6500*0.00003 = 0.195 -> 0.20
        # GST 18% of (20.00 + 0.01 + 2.31 + 0.00) = 4.0176 -> 4.02 | total 26.54
        c = self.e.leg_costs(leg(BUY, "100"))
        self.assertEqual(dict(c.components), {
            "brokerage": Dec("20.00"), "stt": Dec("0.00"), "exchange_txn": Dec("2.31"), "ipft": Dec("0.00"),
            "sebi_fee": Dec("0.01"), "stamp_duty": Dec("0.20"), "gst": Dec("4.02")})
        self.assertEqual(c.total, Dec("26.54"))

    def test_sell_leg_by_hand(self):
        # STT 0.15% x 6,500 = 9.75, no stamp duty; the rest as for the buy
        c = self.e.leg_costs(leg(SELL, "100"))
        self.assertEqual(c.components["stt"], Dec("9.75"))
        self.assertEqual(c.components["stamp_duty"], Dec("0.00"))
        self.assertEqual(c.total, Dec("36.09"))

    def test_rates_follow_the_trade_date(self):
        fx = eng.load_cost_engine("zerodha", "fixture")
        # before 2026-04-01 STT was 0.10%: 6,500 x 0.001 = 6.50
        self.assertEqual(fx.leg_costs(leg(SELL, "100", day=D(2026, 3, 15))).components["stt"], Dec("6.50"))
        self.assertEqual(fx.leg_costs(leg(SELL, "100", day=D(2026, 4, 1))).components["stt"], Dec("9.75"))
        # before 2026-03-01 the exchange charge was 3,503 per crore and IPFT 50: 6500*3503/1e7 = 2.277 -> 2.28
        old = fx.leg_costs(leg(BUY, "100", day=D(2026, 2, 27)))
        self.assertEqual(old.components["exchange_txn"], Dec("2.28"))
        self.assertEqual(old.components["ipft"], Dec("0.03"))                 # 6500*50/1e7 = 0.0325 -> 0.03
        # total exchange outflow is the same either side of the change (3,553 per crore)
        new = fx.leg_costs(leg(BUY, "100", day=D(2026, 3, 2)))
        self.assertEqual(old.components["exchange_txn"] + old.components["ipft"],
                         new.components["exchange_txn"] + new.components["ipft"])

    def test_gst_is_not_charged_on_stt_or_stamp_duty(self):
        c = self.e.leg_costs(leg(SELL, "400", units=650))                     # large, so rounding is not hiding it
        base = c.components["brokerage"] + c.components["sebi_fee"] + c.components["exchange_txn"] + c.components["ipft"]
        self.assertEqual(c.components["gst"], (base * Dec("0.18")).quantize(Dec("0.01")))

    def test_each_cost_carries_its_rule_versions(self):
        c = self.e.leg_costs(leg(SELL, "100"))
        eff_from, verified_on, sources = c.rule_versions["stt_option_sell_premium_rate"]
        self.assertEqual(eff_from, D(2026, 4, 1))
        self.assertEqual(verified_on, D(2026, 11, 4))
        self.assertIn("NSE_FATAX_73524", sources)

    def test_bad_executions_are_refused(self):
        for args in (("HOLD", "100", 65), (BUY, "0", 65), (BUY, "-1", 65), (BUY, "100", 0),
                     (BUY, "100", 65.5), (BUY, "100", True)):
            with self.assertRaises(NiftyError, msg=str(args)):
                LegExecution("L", args[0], Dec(args[1]), args[2], TODAY)
        with self.assertRaises(NiftyError):
            LegExecution("L", BUY, Dec(1), 65, "2026-11-04")


class SpreadCostTests(NoNetworkTestCase):
    def setUp(self):
        super(SpreadCostTests, self).setUp()
        self.e = eng.load_cost_engine("zerodha", REAL)
        self.quoted = [eng.QuotedLeg("long_ce", BUY, Dec("150"), 65), eng.QuotedLeg("short_ce", SELL, Dec("100"), 65)]

    def test_a_spread_is_costed_leg_by_leg_not_as_a_flat_amount(self):
        entry = self.e.entry_costs([leg(BUY, "150", leg_id="a"), leg(SELL, "100", leg_id="b")])
        self.assertEqual(len(entry.legs), 2)
        self.assertEqual(entry.components["brokerage"], Dec("40.00"))         # 2 legs x Rs 20
        self.assertEqual(entry.total, sum(l.total for l in entry.legs))
        # the buy leg pays stamp duty and no STT; the sell leg pays STT and no stamp duty
        self.assertGreater(entry.legs[0].components["stamp_duty"], 0)
        self.assertEqual(entry.legs[0].components["stt"], 0)
        self.assertGreater(entry.legs[1].components["stt"], 0)
        self.assertEqual(entry.legs[1].components["stamp_duty"], 0)

    def test_round_trip_is_entry_plus_exit(self):
        entry = [leg(BUY, "150", leg_id="a"), leg(SELL, "100", leg_id="b")]
        exit_ = [leg(SELL, "160", leg_id="a"), leg(BUY, "95", leg_id="b")]
        rt = self.e.round_trip(entry, exit_)
        self.assertEqual(rt.total, rt.entry.total + rt.exit.total)
        self.assertEqual(rt.exit.components["brokerage"], Dec("40.00"))

    def test_actual_costs_from_paper_fills_use_the_fill_prices(self):
        fills = [leg(BUY, "150.55", leg_id="a"), leg(SELL, "99.45", leg_id="b")]
        actual = self.e.actual_costs(fills)
        estimate = self.e.entry_costs([leg(BUY, "150", leg_id="a"), leg(SELL, "100", leg_id="b")])
        self.assertEqual(actual.label, "actual (paper fills)")
        self.assertNotEqual(actual.total, estimate.total)
        self.assertEqual(actual.total, self.e.phase_costs(fills).total)

    def test_defined_risk_by_hand(self):
        # slippage 0.5 point per leg: buy fills 150.50, sell fills 99.50
        # quoted debit (150-100) x 65 = 3,250.00 | paid (150.50-99.50) x 65 = 3,315.00 | slippage 65.00
        r = self.e.defined_risk(self.quoted, Dec("0.5"), TODAY, Dec("0.05"))
        self.assertEqual(r.net_debit_quoted_inr, Dec("3250.00"))
        self.assertEqual(r.net_debit_paid_inr, Dec("3315.00"))
        self.assertEqual(r.entry_slippage_inr, Dec("65.00"))
        # entry costs, worked out from the formulas on the slipped fills
        buy_to = Dec("150.50") * 65
        sell_to = Dec("99.50") * 65
        brokerage = Dec("40")
        stt = (sell_to * Dec("0.0015")).quantize(Dec("0.01"))
        txn = sum(((t * Dec("3552.99") / 10000000).quantize(Dec("0.01")) for t in (buy_to, sell_to)), Dec(0))
        ipft = sum(((t * Dec("0.01") / 10000000).quantize(Dec("0.01")) for t in (buy_to, sell_to)), Dec(0))
        sebi = sum(((t * 10 / 10000000).quantize(Dec("0.01")) for t in (buy_to, sell_to)), Dec(0))
        stamp = (buy_to * Dec("0.00003")).quantize(Dec("0.01"))
        gst = sum((((Dec("20") + (t * 10 / 10000000).quantize(Dec("0.01"))
                     + (t * Dec("3552.99") / 10000000).quantize(Dec("0.01"))
                     + (t * Dec("0.01") / 10000000).quantize(Dec("0.01"))) * Dec("0.18")).quantize(Dec("0.01"))
                   for t in (buy_to, sell_to)), Dec(0))
        self.assertEqual(r.entry_costs.total, brokerage + stt + txn + ipft + sebi + stamp + gst)
        self.assertEqual(r.entry_risk_inr, r.net_debit_paid_inr + r.entry_costs.total)

    def test_entry_risk_round_trip_costs_and_total_risk_are_distinct(self):
        r = self.e.defined_risk(self.quoted, Dec("0.5"), TODAY, Dec("0.05"))
        self.assertEqual(r.round_trip_costs_inr, r.entry_costs.total + r.estimated_exit_costs.total)
        self.assertEqual(r.total_defined_risk_inr, r.entry_risk_inr + r.estimated_exit_costs.total)
        self.assertGreater(r.total_defined_risk_inr, r.entry_risk_inr)
        self.assertGreater(r.entry_risk_inr, r.net_debit_paid_inr)
        self.assertNotEqual(r.entry_risk_inr, r.round_trip_costs_inr)

    def test_total_risk_is_at_least_the_debit_plus_every_cost(self):
        r = self.e.defined_risk(self.quoted, Dec("0.5"), TODAY, Dec("0.05"))
        self.assertGreaterEqual(r.total_defined_risk_inr, r.net_debit_paid_inr + r.round_trip_costs_inr)
        self.assertEqual(r.total_defined_risk_inr, r.net_debit_paid_inr + r.round_trip_costs_inr)

    def test_defined_risk_has_no_stop_loss_input(self):
        import inspect
        params = inspect.signature(self.e.defined_risk).parameters
        self.assertFalse([p for p in params if "stop" in p.lower()])

    def test_exit_method_changes_only_the_exit_estimate(self):
        a = self.e.defined_risk(self.quoted, Dec("0.5"), TODAY, Dec("0.05"), "entry_price")
        b = self.e.defined_risk(self.quoted, Dec("0.5"), TODAY, Dec("0.05"), "floor_tick")
        self.assertEqual(a.entry_risk_inr, b.entry_risk_inr)
        self.assertGreater(a.estimated_exit_costs.total, b.estimated_exit_costs.total)
        with self.assertRaises(ValueError):
            self.e.defined_risk(self.quoted, Dec("0.5"), TODAY, Dec("0.05"), "optimistic")

    def test_zero_slippage_and_slippage_floor(self):
        r = self.e.defined_risk(self.quoted, Dec("0"), TODAY, Dec("0.05"))
        self.assertEqual(r.entry_slippage_inr, Dec("0.00"))
        self.assertEqual(eng.slipped_price(Dec("0.20"), SELL, Dec("0.5"), Dec("0.05")), Dec("0.05"))
        self.assertEqual(eng.slipped_price(Dec("0.20"), BUY, Dec("0.5"), Dec("0.05")), Dec("0.70"))


class BrokerSeparationTests(NoNetworkTestCase):
    def profile(self, scheme):
        doc = load_json(brokers.BROKER_DIR + "/zerodha.json")
        doc["broker"] = "testbroker"
        doc["items"]["option_brokerage"]["versions"][0]["value"] = scheme
        return brokers.BrokerProfile(doc)

    def engine(self, scheme):
        return eng.CostEngine(statutory.StatutoryCharges.load(), self.profile(scheme), REAL)

    def test_statutory_charges_do_not_depend_on_the_broker(self):
        base = self.engine({"scheme": "flat_per_executed_order", "amount_inr": 20})
        other = self.engine({"scheme": "zero"})
        a, b = base.leg_costs(leg(SELL, "100")), other.leg_costs(leg(SELL, "100"))
        for k in ("stt", "exchange_txn", "ipft", "sebi_fee", "stamp_duty"):
            self.assertEqual(a.components[k], b.components[k], k)
        self.assertEqual(b.components["brokerage"], Dec("0.00"))
        self.assertLess(b.components["gst"], a.components["gst"])             # GST base includes brokerage

    def test_percentage_capped_scheme(self):
        e = self.engine({"scheme": "lower_of_pct_or_flat", "pct": 0.0003, "cap_inr": 20})
        self.assertEqual(e.leg_costs(leg(BUY, "100")).components["brokerage"], Dec("1.95"))   # 6500 x 0.03%
        self.assertEqual(e.leg_costs(leg(BUY, "1000", units=650)).components["brokerage"], Dec("20.00"))

    def test_unknown_scheme_is_refused(self):
        with self.assertRaises(ContractError):
            self.engine({"scheme": "mystery"}).leg_costs(leg(BUY, "100"))

    def test_debit_balance_surcharge_is_per_executed_order(self):
        e = eng.load_cost_engine("zerodha", REAL, account_in_debit=True)
        self.assertEqual(e.leg_costs(leg(BUY, "100")).components["brokerage"], Dec("40.00"))

    def test_the_default_broker_is_zerodha_and_others_load_by_name_or_path(self):
        self.assertEqual(brokers.BrokerProfile.load("zerodha").broker, "zerodha")
        self.assertEqual(brokers.BrokerProfile.load(brokers.BROKER_DIR + "/zerodha.json").broker, "zerodha")
        self.assertTrue(brokers.BrokerProfile.load("zerodha").model_assumptions)


class VerificationGateTests(NoNetworkTestCase):
    def test_real_mode_works_on_a_current_date_and_reports_every_rate(self):
        e = eng.load_cost_engine("zerodha", REAL)
        e.assert_ready(TODAY)
        rows = e.verification_report(TODAY)
        names = [r[0] for r in rows]
        self.assertIn("stt_option_sell_premium_rate", names)
        self.assertIn("zerodha:option_brokerage", names)
        for name, status, problem, warns in rows:
            self.assertIn(status, ("VERIFIED_PRIMARY", "VERIFIED_BROKER_TARIFF"), name)
            self.assertIsNone(problem)

    def test_real_mode_refuses_dates_before_the_start_of_what_was_verified(self):
        e = eng.load_cost_engine("zerodha", REAL)
        with self.assertRaises(CostRateUnverified) as cm:
            e.leg_costs(leg(BUY, "100", day=D(2025, 11, 4)))
        self.assertTrue(cm.exception.missing)
        self.assertIn("sebi_turnover_fee_inr_per_crore", cm.exception.missing)   # start date unknown
        self.assertIn("zerodha:option_brokerage", cm.exception.missing)
        self.assertNotIn("stt_option_sell_premium_rate", cm.exception.missing)   # that one IS dated and verified
        with self.assertRaises(CostRateUnverified):
            e.assert_ready(D(2026, 2, 1))

    def test_fixture_mode_extrapolates_with_warnings_but_cannot_invent_missing_history(self):
        fx = eng.load_cost_engine("zerodha", "fixture")
        c = fx.leg_costs(leg(BUY, "100", day=D(2026, 3, 15)))
        self.assertTrue(any("start date unknown" in w for w in c.warnings))
        with self.assertRaises(CostRateUnverified):                           # STT rate before 2024-10-01 not verified
            fx.leg_costs(leg(SELL, "100", day=D(2024, 6, 3)))

    def test_an_unverified_rate_blocks_real_mode_and_is_named(self):
        doc = load_json(statutory.DEFAULT_PATH)
        v = doc["items"]["sebi_turnover_fee_inr_per_crore"]["versions"][0]
        v.update(status="UNVERIFIED", sources=[], verified_on=None)
        st = statutory.StatutoryCharges(doc)
        broker = brokers.BrokerProfile.load("zerodha")
        real = eng.CostEngine(st, broker, REAL)
        with self.assertRaises(CostRateUnverified) as cm:
            real.leg_costs(leg(BUY, "100"))
        self.assertIn("sebi_turnover_fee_inr_per_crore", cm.exception.missing)
        # fixture mode may still use it, labelled
        fx = eng.CostEngine(st, broker, "fixture")
        c = fx.leg_costs(leg(BUY, "100"))
        self.assertTrue(any("UNVERIFIED" in w for w in c.warnings))

    def test_a_missing_statutory_item_or_source_is_a_hard_error(self):
        doc = load_json(statutory.DEFAULT_PATH)
        del doc["items"]["gst_rate"]
        with self.assertRaises(ContractError):
            statutory.StatutoryCharges(doc)
        doc = load_json(statutory.DEFAULT_PATH)
        doc["items"]["gst_rate"]["versions"][0]["sources"] = ["MADE_UP"]
        with self.assertRaises(ContractError):
            statutory.StatutoryCharges(doc)

    def test_the_cost_engine_has_no_flat_per_spread_entry_point(self):
        self.assertFalse([n for n in dir(eng.CostEngine) if "flat" in n.lower() or "per_spread" in n.lower()])


if __name__ == "__main__":
    unittest.main()
