import copy
import datetime
import unittest
from decimal import Decimal as Dec

from nifty_core import config as config_mod
from nifty_core.data import models as m
from nifty_core.data import validation as v
from nifty_core.errors import DataNotUsable, DataValidationError
from nifty_core.market.ist import IST
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase

D = datetime.date
REAL = "real_readonly"


def at(day, hhmm):
    h, mi = hhmm.split(":")
    return datetime.datetime(day.year, day.month, day.day, int(h), int(mi), tzinfo=IST)


def pick(chain, expiry, strike, kind):
    return m.OptionContract("NIFTY", expiry, strike, kind)


class ContractRuleTests(NoNetworkTestCase):
    def test_lot_size_comes_only_from_the_contract_layer(self):
        specs = sc.specs()
        c = pick(None, D(2026, 9, 22), 25000, "CE")
        self.assertEqual(v.lot_size_for(c, specs, "fixture"), 65)
        self.assertEqual(v.lot_size_for(c, specs, REAL), 65)
        old = pick(None, D(2025, 12, 30), 25000, "CE")
        self.assertEqual(v.lot_size_for(old, specs, "fixture"), 75)

    def test_chain_validation_checks_strike_grid_tick_and_expiry(self):
        specs, cal = sc.specs(), sc.calendar()
        base = sc.scenario("bullish_trend").chains[0]
        v.validate_chain_against_contracts(base, specs, cal, "fixture")          # the clean fixture passes

        def chain_with(**kw):
            q = base.quotes[0]
            contract = m.OptionContract("NIFTY", kw.get("expiry", q.contract.expiry),
                                        kw.get("strike", q.contract.strike), q.contract.option_type)
            quote = m.OptionQuote(contract, q.timestamp, kw.get("bid", q.bid), kw.get("ask", q.ask), q.ltp)
            return m.OptionChain("NIFTY", base.snapshot_time, base.spot, [quote], m.LABEL_FIXTURE)

        cases = (({"strike": Dec(25025)}, "OFF_GRID_STRIKE"),
                 ({"bid": Dec("100.01"), "ask": Dec("101.00")}, "OFF_TICK"),
                 ({"expiry": D(2026, 9, 25)}, "UNKNOWN_EXPIRY"),
                 ({"expiry": D(2026, 9, 15)}, "EXPIRED_CONTRACT"))
        for kw, code in cases:
            with self.assertRaises(DataValidationError, msg=code) as cm:
                v.validate_chain_against_contracts(chain_with(**kw), specs, cal, "fixture")
            self.assertEqual(cm.exception.code, code)

    def test_expiry_with_no_verified_lot_size_is_rejected(self):
        specs, cal = sc.specs(), sc.calendar()
        base = sc.scenario("bullish_trend").chains[0]
        q = base.quotes[0]
        snap = at(D(2025, 9, 8), "10:00")
        # 2025-09-09 is a real NIFTY expiry, but it predates the lot-size circular the contract layer holds
        contract = m.OptionContract("NIFTY", D(2025, 9, 9), q.contract.strike, "CE")
        quote = m.OptionQuote(contract, snap, q.bid, q.ask, q.ltp)
        chain = m.OptionChain("NIFTY", snap, base.spot, [quote], m.LABEL_FIXTURE)
        with self.assertRaises(DataValidationError) as cm:
            v.validate_chain_against_contracts(chain, specs, cal, "fixture")
        self.assertEqual(cm.exception.code, "LOT_SIZE_UNAVAILABLE")


class ExpiryUseTests(NoNetworkTestCase):
    def setUp(self):
        super(ExpiryUseTests, self).setUp()
        self.cal = sc.calendar()
        self.c = m.OptionContract("NIFTY", D(2026, 9, 22), 25000, "CE")

    def test_expired_contracts_cannot_be_used_for_a_new_entry(self):
        self.assertTrue(v.contract_expired(self.c, at(D(2026, 9, 23), "09:30"), self.cal))
        with self.assertRaises(DataValidationError) as cm:
            v.assert_not_expired_for_entry(self.c, at(D(2026, 9, 23), "09:30"), self.cal)
        self.assertEqual(cm.exception.code, "EXPIRED_CONTRACT")

    def test_expiry_day_is_open_until_the_session_close(self):
        self.assertFalse(v.contract_expired(self.c, at(D(2026, 9, 22), "09:30"), self.cal))
        self.assertFalse(v.contract_expired(self.c, at(D(2026, 9, 22), "15:39"), self.cal))
        self.assertTrue(v.contract_expired(self.c, at(D(2026, 9, 22), "15:40"), self.cal))   # session closes 15:40
        v.assert_not_expired_for_entry(self.c, at(D(2026, 9, 21), "15:00"), self.cal)

    def test_a_contract_dated_on_a_closed_day_is_treated_as_expired(self):
        closed = m.OptionContract("NIFTY", D(2026, 11, 10), 25000, "CE")                       # exchange holiday
        self.assertTrue(v.contract_expired(closed, at(D(2026, 11, 10), "10:00"), self.cal))


class EntryGateTests(NoNetworkTestCase):
    """check_entry_data: missing or unverified data blocks a new entry."""

    def setUp(self):
        super(EntryGateTests, self).setUp()
        self.specs, self.cal, self.cfg = sc.specs(), sc.calendar(), sc.config()
        self.chain = sc.scenario("bullish_trend").chains[1]                                     # 10:30 snapshot
        self.now = self.chain.snapshot_time
        self.expiry = D(2026, 9, 22)
        spot = int(self.chain.spot / 50) * 50
        self.long_leg = (m.OptionContract("NIFTY", self.expiry, spot, "CE"), "BUY")
        self.short_leg = (m.OptionContract("NIFTY", self.expiry, spot + 100, "CE"), "SELL")

    def check(self, chain=None, legs=None, now=None, mode="fixture"):
        return v.check_entry_data(chain or self.chain, legs or [self.long_leg, self.short_leg],
                                  now or self.now, self.cfg, self.specs, self.cal, mode)

    def test_clean_data_passes_in_fixture_mode_and_reports_the_lot_size(self):
        r = self.check()
        self.assertTrue(r.ok, r.problems)
        self.assertEqual(r.lot_sizes[self.expiry], 65)

    def test_synthetic_fixture_data_is_blocked_in_a_real_mode(self):
        r = self.check(mode=REAL)
        self.assertFalse(r.ok)
        self.assertIn("DATA_UNVERIFIED", [p[0] for p in r.problems])
        with self.assertRaises(DataNotUsable) as cm:
            v.require_entry_data(self.chain, [self.long_leg, self.short_leg], self.now, self.cfg, self.specs,
                                 self.cal, REAL)
        self.assertIn("DATA_UNVERIFIED", [p[0] for p in cm.exception.problems])

    def test_recorded_real_data_is_not_blocked_by_the_label_rule(self):
        real = m.OptionChain(self.chain.underlying, self.chain.snapshot_time, self.chain.spot,
                             self.chain.quotes, m.LABEL_RECORDED, "recorded")
        r = self.check(chain=real, mode=REAL)
        self.assertNotIn("DATA_UNVERIFIED", [p[0] for p in r.problems])

    def test_missing_quote_blocks(self):
        far = (m.OptionContract("NIFTY", self.expiry, 40000, "CE"), "BUY")
        r = self.check(legs=[far, self.short_leg])
        self.assertIn("MISSING_QUOTE", [p[0] for p in r.problems])

    def test_a_leg_needs_the_side_it_will_trade(self):
        q = self.chain.get(self.long_leg[0])
        no_ask = m.OptionQuote(q.contract, q.timestamp, q.bid, None, q.ltp)
        zero_bid = m.OptionQuote(self.short_leg[0], self.chain.snapshot_time, "0.00", "0.05", "0.05")
        chain = m.OptionChain("NIFTY", self.chain.snapshot_time, self.chain.spot, [no_ask, zero_bid], m.LABEL_FIXTURE)
        r = self.check(chain=chain)
        codes = [p[0] for p in r.problems]
        self.assertIn("NO_ASK", codes)                  # cannot BUY without an ask
        self.assertIn("NO_BID", codes)                  # cannot SELL into a zero bid

    def test_stale_quotes_block(self):
        stale_now = self.now + datetime.timedelta(seconds=self.cfg.data_stale_after_seconds + 1)
        r = self.check(now=stale_now)
        self.assertIn("STALE_DATA", [p[0] for p in r.problems])
        fresh = self.check(now=self.now + datetime.timedelta(seconds=self.cfg.data_stale_after_seconds))
        self.assertNotIn("STALE_DATA", [p[0] for p in fresh.problems])

    def test_a_snapshot_from_the_future_blocks(self):
        r = self.check(now=self.now - datetime.timedelta(minutes=5))
        self.assertIn("QUOTE_FROM_FUTURE", [p[0] for p in r.problems])

    def test_expired_contract_blocks(self):
        gone = (m.OptionContract("NIFTY", self.expiry, int(self.chain.spot / 50) * 50, "CE"), "BUY")
        after_close = at(self.expiry, "15:45")
        chain = m.OptionChain("NIFTY", after_close, self.chain.spot, [], m.LABEL_FIXTURE)
        r = v.check_entry_data(chain, [gone], after_close, self.cfg, self.specs, self.cal, "fixture")
        self.assertIn("EXPIRED_CONTRACT", [p[0] for p in r.problems])

    def test_unverified_lot_size_blocks(self):
        gap = (m.OptionContract("NIFTY", D(2026, 1, 1), 25000, "CE"), "BUY")           # lot-size gap date
        r = v.check_entry_data(self.chain, [gap], self.now, self.cfg, self.specs, self.cal, "fixture")
        self.assertIn("LOT_SIZE_UNVERIFIED", [p[0] for p in r.problems])

    def test_a_bad_side_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            v.check_entry_data(self.chain, [(self.long_leg[0], "HOLD")], self.now, self.cfg, self.specs, self.cal,
                               "fixture")

    def test_real_mode_blocks_scenarios_whose_contract_data_is_not_verified_for_the_date(self):
        # the contract layer's unknown-start values are only trusted from 2026-11-04; a September
        # fixture date cannot silently pass as "real" data
        from nifty_core.errors import ContractError
        from nifty_core.data.fixture_provider import load_scenario_file
        import os
        with self.assertRaises(ContractError):
            load_scenario_file(os.path.join(sc.SCEN_DIR, "bullish_trend.json"), self.specs,
                               sc.calendar(REAL), REAL)


if __name__ == "__main__":
    unittest.main()
