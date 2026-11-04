import copy
import datetime
import json
import os
import unittest
from decimal import Decimal as Dec

from nifty_core.data import models as m
from nifty_core.data.fixture_provider import FixtureProvider, load_scenario_file, parse_scenario
from nifty_core.errors import DataNotAvailable, DataValidationError
from nifty_core.market.ist import FixedClock, IST
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase

D = datetime.date


def at(day, hhmm):
    h, mi = hhmm.split(":")
    return datetime.datetime(day.year, day.month, day.day, int(h), int(mi), tzinfo=IST)


class LoadingTests(NoNetworkTestCase):
    def test_every_scenario_file_loads_and_is_labelled_synthetic(self):
        for name in sc.NAMES:
            s = sc.scenario(name)
            self.assertEqual(s.data_label, m.LABEL_FIXTURE, name)
            self.assertEqual(s.underlying, "NIFTY")
            self.assertIn("synthetic_notice", s.meta)
            for ch in s.chains:
                self.assertEqual(ch.data_label, m.LABEL_FIXTURE)

    def test_session_shape_and_counts(self):
        for name in sc.NAMES:
            s = sc.scenario(name)
            expected = 73 if name == "missing_invalid_data" else 77        # 09:15 to 15:40 in 5-minute bars
            self.assertEqual(len(s.candles), expected, name)
            self.assertEqual(s.candles[0].start.time(), datetime.time(9, 15))
            self.assertEqual(s.candles[-1].end.time() if name != "missing_invalid_data" else datetime.time(15, 40),
                             datetime.time(15, 40))
        self.assertEqual(len(sc.scenario("missing_invalid_data").chains), 3)
        self.assertEqual(len(sc.scenario("bullish_trend").chains), 4)

    def test_scenario_days_are_real_trading_days_with_real_expiries(self):
        from nifty_core.market import expiry as ex
        for name in sc.NAMES:
            s = sc.scenario(name)
            self.assertTrue(sc.calendar().is_trading_day(s.day), name)
            for e in s.chains[0].expiries():
                self.assertTrue(ex.is_expiry_day(e, sc.calendar(), sc.specs(), "fixture"), (name, e))

    def test_high_volatility_scenario_uses_a_holiday_shifted_expiry(self):
        s = sc.scenario("high_volatility")
        self.assertIn(D(2026, 10, 19), s.chains[0].expiries())             # Dussehra moved Oct 20 to Oct 19

    def test_no_lot_size_is_stored_in_any_fixture_file(self):
        for folder in (sc.SCEN_DIR,):
            for name in os.listdir(folder):
                with open(os.path.join(folder, name), encoding="utf-8") as fh:
                    text = fh.read().lower()
                self.assertNotIn("lot_size", text, name)
                self.assertNotIn('"lot"', text, name)

    def test_quote_quantities_are_whole_lots_of_the_layers_lot_size(self):
        lot = sc.specs().lot_size(D(2026, 9, 22)).value
        for q in sc.scenario("bullish_trend").chains[0].quotes[:40]:
            self.assertEqual(q.bid_qty % lot, 0)
            self.assertEqual(q.ask_qty % lot, 0)

    def test_a_scenario_cannot_smuggle_a_lot_size(self):
        with open(os.path.join(sc.INVALID_DIR, "invalid_lot_size_in_data.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        with self.assertRaises(DataValidationError) as cm:
            parse_scenario(doc, sc.specs(), sc.calendar())
        self.assertEqual(cm.exception.code, "LOT_SIZE_IN_DATA")

    def test_bad_schema_label_and_ordering_are_rejected(self):
        with open(os.path.join(sc.SCEN_DIR, "sideways.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        for mutate, code in ((lambda d: d.update(schema_version=9), "BAD_SCHEMA"),
                             (lambda d: d.update(data_label="REAL"), "BAD_LABEL"),
                             (lambda d: d["candles"].reverse(), "CANDLE_ORDER"),
                             (lambda d: d["chains"].reverse(), "CHAIN_ORDER"),
                             (lambda d: d.update(date="2026-09-26"), "MARKET_CLOSED")):       # a Saturday
            d = copy.deepcopy(doc)
            mutate(d)
            with self.assertRaises(DataValidationError, msg=code) as cm:
                parse_scenario(d, sc.specs(), sc.calendar())
            self.assertEqual(cm.exception.code, code)


class ProviderBehaviourTests(NoNetworkTestCase):
    def setUp(self):
        super(ProviderBehaviourTests, self).setUp()
        self.day = D(2026, 9, 16)
        self.p = sc.provider("bullish_trend")

    def test_bars_include_only_finished_candles(self):
        bars = self.p.get_underlying_bars(5, at(self.day, "09:15"), at(self.day, "15:40"), as_of=at(self.day, "10:00"))
        self.assertEqual(len(bars), 9)                                   # 09:15 .. 09:55 starts, last ends 10:00
        self.assertEqual(bars[-1].end, at(self.day, "10:00"))
        partial = self.p.get_underlying_bars(5, at(self.day, "09:15"), at(self.day, "15:40"), as_of=at(self.day, "10:03"))
        self.assertEqual(len(partial), 9)                                # the 10:00 bar has not finished at 10:03

    def test_bars_respect_the_requested_window(self):
        bars = self.p.get_underlying_bars(5, at(self.day, "10:00"), at(self.day, "10:30"), as_of=at(self.day, "15:40"))
        self.assertEqual([b.start.strftime("%H:%M") for b in bars],
                         ["10:00", "10:05", "10:10", "10:15", "10:20", "10:25"])

    def test_wrong_interval_is_not_available(self):
        with self.assertRaises(DataNotAvailable):
            self.p.get_underlying_bars(1, at(self.day, "09:15"), at(self.day, "10:00"))

    def test_no_look_ahead_in_option_chains(self):
        with self.assertRaises(DataNotAvailable):
            self.p.get_option_chain(as_of=at(self.day, "09:30"))
        self.assertEqual(self.p.get_option_chain(as_of=at(self.day, "09:45")).snapshot_time, at(self.day, "09:45"))
        self.assertEqual(self.p.get_option_chain(as_of=at(self.day, "10:29")).snapshot_time, at(self.day, "09:45"))
        self.assertEqual(self.p.get_option_chain(as_of=at(self.day, "10:30")).snapshot_time, at(self.day, "10:30"))
        self.assertEqual(self.p.get_option_chain(as_of=at(self.day, "15:40")).snapshot_time, at(self.day, "14:30"))

    def test_the_default_moment_is_the_provider_clock(self):
        clock = FixedClock(at(self.day, "10:31"))
        p = FixtureProvider(sc.scenario("bullish_trend"), clock)
        self.assertEqual(p.get_clock(), at(self.day, "10:31"))
        self.assertEqual(p.get_option_chain().snapshot_time, at(self.day, "10:30"))
        clock.set(at(self.day, "12:31"))
        self.assertEqual(p.get_option_chain().snapshot_time, at(self.day, "12:30"))

    def test_single_expiry_view_and_expiries(self):
        e1, e2 = sc.scenario("bullish_trend").meta["expiries"]
        view = self.p.get_option_chain(expiry=D.fromisoformat(e2), as_of=at(self.day, "10:30"))
        self.assertEqual(view.expiries(), [D.fromisoformat(e2)])
        self.assertEqual(view.snapshot_time, at(self.day, "10:30"))
        with self.assertRaises(DataNotAvailable):
            self.p.get_option_chain(expiry=D(2030, 1, 1), as_of=at(self.day, "10:30"))
        self.assertEqual(self.p.get_expiries(as_of=at(self.day, "10:30")), [D.fromisoformat(e1), D.fromisoformat(e2)])

    def test_get_quote(self):
        chain = self.p.get_option_chain(as_of=at(self.day, "10:30"))
        q0 = chain.quotes[0]
        self.assertEqual(self.p.get_quote(q0.contract, as_of=at(self.day, "10:30")), q0)
        with self.assertRaises(DataNotAvailable):
            self.p.get_quote(m.OptionContract("NIFTY", D(2026, 9, 22), 40000, "CE"), as_of=at(self.day, "10:30"))
        with self.assertRaises(DataNotAvailable):
            self.p.get_quote(q0.contract, as_of=at(self.day, "09:00"))

    def test_event_calendar(self):
        hv = sc.provider("high_volatility")
        events = hv.get_event_calendar(D(2026, 10, 7))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].impact, "HIGH")
        self.assertEqual(events[0].when, at(D(2026, 10, 7), "11:45"))
        self.assertEqual(hv.get_event_calendar(D(2026, 10, 8)), [])
        self.assertEqual(self.p.get_event_calendar(self.day), [])


class DeterminismAndReadOnlyTests(NoNetworkTestCase):
    def test_same_scenario_same_answers_every_time(self):
        a = sc.provider("sideways")
        b = FixtureProvider(load_scenario_file(os.path.join(sc.SCEN_DIR, "sideways.json"), sc.specs(), sc.calendar()))
        day = D(2026, 9, 23)
        for t in ("09:45", "10:30", "14:30"):
            self.assertEqual(a.get_option_chain(as_of=at(day, t)).quotes, b.get_option_chain(as_of=at(day, t)).quotes)
            self.assertEqual(a.get_option_chain(as_of=at(day, t)).quotes, a.get_option_chain(as_of=at(day, t)).quotes)
        self.assertEqual(a.get_underlying_bars(5, at(day, "09:15"), at(day, "15:40"), at(day, "15:40")),
                         b.get_underlying_bars(5, at(day, "09:15"), at(day, "15:40"), at(day, "15:40")))

    def test_returned_data_cannot_be_modified(self):
        p = sc.provider("bullish_trend")
        day = D(2026, 9, 16)
        chain = p.get_option_chain(as_of=at(day, "10:30"))
        with self.assertRaises(AttributeError):
            chain.spot = Dec(1)
        with self.assertRaises(AttributeError):
            chain.quotes[0].bid = Dec(1)
        with self.assertRaises((TypeError, AttributeError)):
            chain.quotes[0] = None
        bars = p.get_underlying_bars(5, at(day, "09:15"), at(day, "15:40"), at(day, "10:00"))
        with self.assertRaises(TypeError):
            bars[0] = None


if __name__ == "__main__":
    unittest.main()
