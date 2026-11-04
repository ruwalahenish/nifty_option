"""The six scenarios behave as described, and every invalid fixture is rejected for its stated reason."""
import json
import math
import os
import statistics
import unittest
from decimal import ROUND_HALF_UP, Decimal as Dec

from nifty_core.data import validation as v
from nifty_core.data.models import OptionContract
from nifty_core.data.fixture_provider import parse_scenario
from nifty_core.errors import DataValidationError
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase
from tools import make_fixtures


def bar_returns(s):
    closes = [c.close for c in s.candles]
    return [math.log(float(b / a)) for a, b in zip(closes, closes[1:])]


def day_return_pct(s):
    return float((s.candles[-1].close / s.candles[0].open - 1) * 100)


def range_pct(s):
    return float((max(c.high for c in s.candles) - min(c.low for c in s.candles)) / s.candles[0].open * 100)


def nearest_strike(chain):
    """The strike the generator treats as at-the-money: spot / 50, rounded half up."""
    return int((chain.spot / 50).to_integral_value(ROUND_HALF_UP)) * 50


def near_atm(chain, expiry, kind, width=100):
    atm = nearest_strike(chain)
    return [q for q in chain.for_expiry(expiry) if q.contract.option_type == kind and abs(q.contract.strike - atm) <= width]


class IndexBehaviourTests(NoNetworkTestCase):
    def test_bullish_trend(self):
        s = sc.scenario("bullish_trend")
        self.assertGreater(day_return_pct(s), 0.5)
        self.assertGreater(s.candles[-1].close, s.candles[5].close)               # above the 09:45 level
        self.assertGreater(s.candles[0].open, Dec(s.meta["previous_close"]))      # gap up
        self.assertEqual(s.meta["scenario_intent"], "BULLISH")

    def test_bearish_trend(self):
        s = sc.scenario("bearish_trend")
        self.assertLess(day_return_pct(s), -0.5)
        self.assertLess(s.candles[-1].close, s.candles[5].close)
        self.assertLess(s.candles[0].open, Dec(s.meta["previous_close"]))         # gap down
        self.assertEqual(s.meta["scenario_intent"], "BEARISH")

    def test_sideways(self):
        s = sc.scenario("sideways")
        self.assertLess(abs(day_return_pct(s)), 0.3)
        self.assertLess(range_pct(s), 0.7)
        self.assertEqual(s.meta["scenario_intent"], "SIDEWAYS")

    def test_high_volatility_has_far_larger_bar_moves_than_the_calm_days(self):
        hv = statistics.pstdev(bar_returns(sc.scenario("high_volatility")))
        calm = statistics.pstdev(bar_returns(sc.scenario("sideways")))
        self.assertGreater(hv, 2.0 * calm)
        biggest = max(abs(r) for r in bar_returns(sc.scenario("high_volatility")))
        self.assertGreater(biggest, 0.005)                                         # the synthetic announcement jump
        self.assertEqual(sc.scenario("high_volatility").events[0].impact, "HIGH")

    def test_high_volatility_options_are_priced_richer(self):
        calm, wild = sc.scenario("sideways"), sc.scenario("high_volatility")

        def atm_call_mid(s):
            ch = s.chains[0]
            e = ch.expiries()[0]
            atm = near_atm(ch, e, "CE", 0)[0]
            return float(atm.mid)
        # compare per unit of time left: both first expiries are 6 days away
        self.assertGreater(atm_call_mid(wild), 1.5 * atm_call_mid(calm))

    def test_candle_and_snapshot_timestamps_are_ist_and_ordered(self):
        for name in sc.NAMES:
            s = sc.scenario(name)
            starts = [c.start for c in s.candles]
            self.assertEqual(starts, sorted(starts))
            for c in s.candles:
                self.assertEqual(c.start.utcoffset().total_seconds(), 19800)
            snaps = [c.snapshot_time for c in s.chains]
            self.assertEqual(snaps, sorted(snaps))


class ChainRealismTests(NoNetworkTestCase):
    def test_option_prices_are_monotonic_in_strike(self):
        ch = sc.scenario("bullish_trend").chains[0]
        for e in ch.expiries():
            calls = [q.mid for q in ch.for_expiry(e) if q.contract.option_type == "CE"]
            puts = [q.mid for q in ch.for_expiry(e) if q.contract.option_type == "PE"]
            self.assertEqual(calls, sorted(calls, reverse=True), e)
            self.assertEqual(puts, sorted(puts), e)

    def test_chain_is_centred_on_the_spot_and_on_the_strike_grid(self):
        for name in sc.NAMES:
            for ch in sc.scenario(name).chains:
                strikes = ch.strikes(ch.expiries()[0]) if ch.quotes else []
                if not strikes:
                    continue
                for k in strikes:
                    self.assertEqual(k % 50, 0)
                self.assertLessEqual(strikes[0], ch.spot)
                self.assertGreaterEqual(strikes[-1], ch.spot)

    def test_longer_dated_options_cost_more_than_shorter_dated_ones(self):
        ch = sc.scenario("sideways").chains[1]
        e1, e2 = ch.expiries()
        atm1, atm2 = near_atm(ch, e1, "CE", 0)[0], near_atm(ch, e2, "CE", 0)[0]
        self.assertGreater(atm2.mid, atm1.mid)

    def test_liquid_scenarios_have_tight_two_sided_quotes_near_the_money(self):
        for name in ("bullish_trend", "bearish_trend", "sideways", "high_volatility"):
            ch = sc.scenario(name).chains[1]
            for e in ch.expiries():
                for kind in ("CE", "PE"):
                    for q in near_atm(ch, e, kind, 200):
                        self.assertTrue(q.is_two_sided, (name, q.contract.symbol))
                        self.assertLess(q.spread_pct_of_mid, Dec("1.5"), (name, q.contract.symbol))

    def test_every_snapshot_of_the_clean_scenarios_passes_the_quality_check(self):
        for name in ("bullish_trend", "bearish_trend", "sideways", "high_volatility"):
            for ch in sc.scenario(name).chains:
                issues = [i for i in v.assess_quality(ch, ch.snapshot_time, 60, Dec(15))
                          if i.code not in ("ZERO_BID", "WIDE_SPREAD")]
                self.assertEqual(issues, [], (name, ch.snapshot_time))


class IlliquidScenarioTests(NoNetworkTestCase):
    def test_spreads_are_wide_even_near_the_money(self):
        ch = sc.scenario("illiquid_options").chains[1]
        e = ch.expiries()[0]
        pcts = [float(q.spread_pct_of_mid) for q in near_atm(ch, e, "CE", 100) + near_atm(ch, e, "PE", 100)
                if q.spread_pct_of_mid is not None]
        self.assertGreater(statistics.median(pcts), 5.0)

    def test_many_far_quotes_have_no_bid(self):
        ch = sc.scenario("illiquid_options").chains[1]
        zero = [q for q in ch.quotes if q.bid == 0]
        self.assertGreater(len(zero), 10)
        self.assertTrue(all(not q.is_two_sided for q in zero))
        # a spread that needs to SELL into a zero bid is blocked by the entry gate
        e = ch.expiries()[0]
        target = zero[0].contract
        r = v.check_entry_data(ch, [(target, "SELL")], ch.snapshot_time, sc.config(), sc.specs(), sc.calendar(), "fixture")
        self.assertIn("NO_BID", [p[0] for p in r.problems])

    def test_volume_and_open_interest_are_thin_compared_with_liquid_days(self):
        thin = sc.scenario("illiquid_options").chains[1]
        liquid = sc.scenario("sideways").chains[1]

        def total(chain, attr):
            return sum(getattr(q, attr) or 0 for q in chain.quotes)
        self.assertLess(total(thin, "volume") * 20, total(liquid, "volume"))
        self.assertLess(total(thin, "open_interest") * 20, total(liquid, "open_interest"))

    def test_quality_report_flags_the_wide_and_empty_quotes(self):
        ch = sc.scenario("illiquid_options").chains[1]
        codes = {i.code for i in v.assess_quality(ch, ch.snapshot_time, 60, Dec(10))}
        self.assertIn("WIDE_SPREAD", codes)
        self.assertIn("ZERO_BID", codes)


class MissingAndInvalidDataScenarioTests(NoNetworkTestCase):
    def setUp(self):
        super(MissingAndInvalidDataScenarioTests, self).setUp()
        self.s = sc.scenario("missing_invalid_data")

    def test_candle_gaps_are_real_gaps(self):
        starts = [c.start for c in self.s.candles]
        gaps = [(a, b) for a, b in zip(starts, starts[1:]) if (b - a).total_seconds() != 300]
        self.assertEqual(len(gaps), 2)                                             # bars 10-12 and bar 40 are missing
        self.assertEqual(len(self.s.candles), 73)

    def test_the_14_30_snapshot_is_missing(self):
        times = [c.snapshot_time.strftime("%H:%M") for c in self.s.chains]
        self.assertEqual(times, ["09:45", "10:30", "12:30"])
        p = sc.provider("missing_invalid_data")
        last = p.get_option_chain(as_of=self.s.chains[-1].snapshot_time.replace(hour=15))
        self.assertEqual(last.snapshot_time.strftime("%H:%M"), "12:30")            # the caller sees stale data, not a new one

    def test_the_stale_snapshot_is_detected(self):
        ch = self.s.chains[1]
        issues = v.assess_quality(ch, ch.snapshot_time, 60)
        stale = [i for i in issues if i.code == "STALE"]
        self.assertEqual(len(stale), len(ch.quotes))
        r = v.check_entry_data(ch, [(ch.quotes[0].contract, "BUY")], ch.snapshot_time, sc.config(), sc.specs(),
                               sc.calendar(), "fixture")
        self.assertIn("STALE_DATA", [p[0] for p in r.problems])

    def test_one_sided_zero_bid_and_missing_fields_are_flagged(self):
        ch = self.s.chains[2]
        codes = {i.code for i in v.assess_quality(ch, ch.snapshot_time, 60)}
        for expected in ("ONE_SIDED", "ZERO_BID", "MISSING_OPEN_INTEREST", "MISSING_VOLUME"):
            self.assertIn(expected, codes)

    def test_missing_strikes_make_the_atm_call_unavailable(self):
        ch = self.s.chains[2]
        e = min(ch.expiries())
        atm = nearest_strike(ch)
        self.assertEqual([q for q in near_atm(ch, e, "CE", 100)], [])
        missing = OptionContract("NIFTY", e, atm, "CE")
        r = v.check_entry_data(ch, [(missing, "BUY")], ch.snapshot_time, sc.config(), sc.specs(), sc.calendar(), "fixture")
        self.assertIn("MISSING_QUOTE", [p[0] for p in r.problems])

    def test_the_declared_defects_are_listed_in_metadata(self):
        self.assertEqual(len(self.s.meta["defects"]), 4)


class InvalidFixtureFileTests(NoNetworkTestCase):
    def files(self):
        return sorted(f for f in os.listdir(sc.INVALID_DIR) if f.endswith(".json"))

    def test_every_invalid_file_is_rejected_for_its_declared_reason(self):
        files = self.files()
        self.assertEqual(len(files), len(make_fixtures.INVALID_CASES))
        for name in files:
            with open(os.path.join(sc.INVALID_DIR, name), encoding="utf-8") as fh:
                doc = json.load(fh)
            with self.assertRaises(DataValidationError, msg=name) as cm:
                parse_scenario(doc, sc.specs(), sc.calendar())
            self.assertEqual(cm.exception.code, doc["expected_error_code"], name)

    def test_the_required_rules_are_all_covered(self):
        codes = set()
        for name in self.files():
            with open(os.path.join(sc.INVALID_DIR, name), encoding="utf-8") as fh:
                codes.add(json.load(fh)["expected_error_code"])
        for required in ("NAIVE_TIMESTAMP", "DUPLICATE_CONTRACT", "BID_GT_ASK", "NEGATIVE_PRICE", "NEGATIVE_VOLUME",
                         "EXPIRED_CONTRACT", "LOT_SIZE_IN_DATA", "OFF_TICK", "OFF_GRID_STRIKE", "UNKNOWN_EXPIRY",
                         "QUOTE_FROM_FUTURE", "CANDLE_INCONSISTENT"):
            self.assertIn(required, codes)

    def test_the_unmodified_base_of_the_invalid_files_is_valid(self):
        with open(os.path.join(sc.SCEN_DIR, "bullish_trend.json"), encoding="utf-8") as fh:
            base = make_fixtures.mini(json.load(fh))
        parse_scenario(base, sc.specs(), sc.calendar())


class GeneratorDeterminismTests(NoNetworkTestCase):
    def test_regenerating_gives_exactly_the_committed_files(self):
        for path, text in make_fixtures.build_all().items():
            with open(path, encoding="utf-8", newline="") as fh:
                self.assertEqual(fh.read(), text, os.path.relpath(path, sc.ROOT))

    def test_two_runs_are_identical(self):
        self.assertEqual(make_fixtures.build_all(), make_fixtures.build_all())


if __name__ == "__main__":
    unittest.main()
