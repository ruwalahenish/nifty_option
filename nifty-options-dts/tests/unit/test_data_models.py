import datetime
import unittest
from decimal import Decimal as Dec

from nifty_core.data import models as m
from nifty_core.errors import DataValidationError
from nifty_core.market.ist import IST, UTC
from tests._base import NoNetworkTestCase

D = datetime.date
T0 = datetime.datetime(2026, 9, 16, 9, 45, tzinfo=IST)
EXPIRY = D(2026, 9, 22)


def contract(strike=25000, kind="CE", expiry=EXPIRY):
    return m.OptionContract("NIFTY", expiry, strike, kind)


def quote(**kw):
    base = dict(contract=contract(), timestamp=T0, bid="100.00", ask="100.50", ltp="100.25")
    base.update(kw)
    return m.OptionQuote(**base)


class Raises(object):
    def __init__(self, test, code):
        self.test, self.code = test, code

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.test.assertIsNotNone(exc_type, "expected DataValidationError %s" % self.code)
        self.test.assertTrue(issubclass(exc_type, DataValidationError), exc_type)
        self.test.assertEqual(exc.code, self.code)
        return True


class TimestampTests(NoNetworkTestCase):
    def test_naive_timestamps_are_rejected_everywhere(self):
        naive = datetime.datetime(2026, 9, 16, 9, 45)
        with Raises(self, "NAIVE_TIMESTAMP"):
            quote(timestamp=naive)
        with Raises(self, "NAIVE_TIMESTAMP"):
            m.Candle(naive, 5, "100", "101", "99", "100")
        with Raises(self, "NAIVE_TIMESTAMP"):
            m.OptionChain("NIFTY", naive, "25000", [], m.LABEL_FIXTURE)
        with Raises(self, "NAIVE_TIMESTAMP"):
            m.make_event(naive, "x", "LOW", m.LABEL_FIXTURE)

    def test_other_time_zones_are_converted_to_ist(self):
        utc = datetime.datetime(2026, 9, 16, 4, 15, tzinfo=UTC)
        q = quote(timestamp=utc)
        self.assertEqual(q.timestamp, T0)
        self.assertEqual(q.timestamp.utcoffset(), datetime.timedelta(hours=5, minutes=30))

    def test_non_datetimes_are_rejected(self):
        with Raises(self, "BAD_TIMESTAMP"):
            quote(timestamp="2026-09-16T09:45:00+05:30")


class QuoteRuleTests(NoNetworkTestCase):
    def test_bid_cannot_exceed_ask(self):
        with Raises(self, "BID_GT_ASK"):
            quote(bid="101.00", ask="100.50")
        quote(bid="100.50", ask="100.50")                           # locked market is allowed

    def test_negative_prices_and_counts_are_rejected(self):
        for field in ("bid", "ask", "ltp"):
            with Raises(self, "NEGATIVE_PRICE"):
                quote(**{field: "-0.05"})
        with Raises(self, "NEGATIVE_VOLUME"):
            quote(volume=-1)
        with Raises(self, "NEGATIVE_VOLUME"):
            quote(bid_qty=-65)
        with Raises(self, "NEGATIVE_OPEN_INTEREST"):
            quote(open_interest=-1)

    def test_floats_and_junk_numbers_are_rejected(self):
        with Raises(self, "BAD_NUMBER"):
            quote(bid=100.5)                                        # binary floats are never accepted as prices
        with Raises(self, "BAD_NUMBER"):
            quote(ask="abc")
        with Raises(self, "BAD_NUMBER"):
            quote(ask="NaN")
        with Raises(self, "BAD_NUMBER"):
            quote(volume=1.5)
        with Raises(self, "BAD_NUMBER"):
            quote(volume=True)

    def test_missing_fields_are_none_not_zero(self):
        q = quote(bid=None, ask=None, ltp=None)
        self.assertIsNone(q.bid)
        self.assertIsNone(q.volume)
        self.assertFalse(q.is_two_sided)
        self.assertIsNone(q.mid)
        self.assertIsNone(q.spread_pct_of_mid)

    def test_zero_bid_is_allowed_but_not_two_sided(self):
        q = quote(bid="0.00", ask="0.05")
        self.assertFalse(q.is_two_sided)
        self.assertEqual(q.spread, Dec("0.05"))

    def test_derived_values(self):
        q = quote(bid="100.00", ask="101.00")
        self.assertTrue(q.is_two_sided)
        self.assertEqual(q.mid, Dec("100.50"))
        self.assertAlmostEqual(float(q.spread_pct_of_mid), 0.995, places=3)
        later = T0 + datetime.timedelta(seconds=90)
        self.assertEqual(q.age_seconds(later), 90)

    def test_a_quote_has_no_lot_size_field(self):
        for cls in (m.OptionQuote, m.OptionContract, m.Candle):
            self.assertFalse([f for f in cls._fields if "lot" in f.lower()], cls)
        self.assertFalse([f for f in m.OptionChain.__slots__ if "lot" in f.lower()])


class ContractAndCandleTests(NoNetworkTestCase):
    def test_contracts_compare_by_identity_fields(self):
        a, b = contract(), contract()
        self.assertEqual(a, b)
        self.assertEqual(len({a, b}), 1)
        self.assertNotEqual(a, contract(kind="PE"))
        self.assertNotEqual(a, contract(strike=25050))
        self.assertNotEqual(a, contract(expiry=D(2026, 9, 29)))
        self.assertEqual(a.symbol, "NIFTY26092225000CE")

    def test_bad_contracts(self):
        with Raises(self, "BAD_OPTION_TYPE"):
            m.OptionContract("NIFTY", EXPIRY, 25000, "XX")
        with Raises(self, "NEGATIVE_PRICE"):
            m.OptionContract("NIFTY", EXPIRY, -1, "CE")
        with Raises(self, "NEGATIVE_PRICE"):
            m.OptionContract("NIFTY", EXPIRY, 0, "CE")
        with Raises(self, "BAD_TIMESTAMP"):
            m.OptionContract("NIFTY", datetime.datetime(2026, 9, 22, 15, 40, tzinfo=IST), 25000, "CE")
        with Raises(self, "MISSING_REQUIRED"):
            m.OptionContract("", EXPIRY, 25000, "CE")

    def test_candle_consistency(self):
        c = m.Candle(T0, 5, "100.00", "101.00", "99.00", "100.50")
        self.assertEqual(c.end, T0 + datetime.timedelta(minutes=5))
        self.assertIsNone(c.volume)
        for args in (("100", "99", "98", "100"),            # high below open
                     ("100", "101", "99.5", "99"),           # close below low
                     ("100", "101", "99", "102")):           # close above high
            with Raises(self, "CANDLE_INCONSISTENT"):
                m.Candle(T0, 5, *args)
        with Raises(self, "NEGATIVE_PRICE"):
            m.Candle(T0, 5, "0", "1", "0", "1")
        with Raises(self, "NEGATIVE_VOLUME"):
            m.Candle(T0, 5, "100", "101", "99", "100", volume=-1)
        with Raises(self, "BAD_NUMBER"):
            m.Candle(T0, 0, "100", "101", "99", "100")


class ChainTests(NoNetworkTestCase):
    def chain(self, quotes):
        return m.OptionChain("NIFTY", T0, "25000.00", quotes, m.LABEL_FIXTURE, "test")

    def test_duplicate_contracts_in_one_snapshot_are_rejected(self):
        with Raises(self, "DUPLICATE_CONTRACT"):
            self.chain([quote(), quote(bid="99.00")])

    def test_same_strike_different_type_or_expiry_is_not_a_duplicate(self):
        ch = self.chain([quote(), quote(contract=contract(kind="PE")),
                         quote(contract=contract(expiry=D(2026, 9, 29)))])
        self.assertEqual(len(ch.quotes), 3)
        self.assertEqual(ch.expiries(), [EXPIRY, D(2026, 9, 29)])

    def test_quote_after_the_snapshot_is_look_ahead(self):
        with Raises(self, "QUOTE_FROM_FUTURE"):
            self.chain([quote(timestamp=T0 + datetime.timedelta(seconds=1))])

    def test_older_quotes_are_allowed_so_staleness_can_be_detected(self):
        self.chain([quote(timestamp=T0 - datetime.timedelta(minutes=10))])

    def test_wrong_underlying_and_label(self):
        other = m.OptionContract("BANKNIFTY", EXPIRY, 55000, "CE")
        with Raises(self, "WRONG_UNDERLYING"):
            self.chain([quote(contract=other)])
        with Raises(self, "BAD_LABEL"):
            m.OptionChain("NIFTY", T0, "25000", [], "REAL")

    def test_chain_is_immutable_and_sorted(self):
        ch = self.chain([quote(contract=contract(25100)), quote(contract=contract(25000, "PE")), quote()])
        with self.assertRaises(AttributeError):
            ch.spot = Dec(1)
        keys = [(q.contract.strike, q.contract.option_type) for q in ch.quotes]
        self.assertEqual(keys, sorted(keys))
        self.assertIsInstance(ch.quotes, tuple)
        self.assertEqual(ch.get(contract()).bid, Dec("100.00"))
        self.assertIsNone(ch.get(contract(99999)))
        self.assertEqual(ch.strikes(EXPIRY), [Dec(25000), Dec(25100)])

    def test_events(self):
        e = m.make_event(T0, "x", "HIGH", m.LABEL_FIXTURE)
        self.assertEqual(e.impact, "HIGH")
        m.make_event(D(2026, 9, 16), "all day", "LOW", m.LABEL_FIXTURE)
        with Raises(self, "BAD_IMPACT"):
            m.make_event(T0, "x", "HUGE", m.LABEL_FIXTURE)
        with Raises(self, "MISSING_REQUIRED"):
            m.make_event(T0, "", "LOW", m.LABEL_FIXTURE)


if __name__ == "__main__":
    unittest.main()
