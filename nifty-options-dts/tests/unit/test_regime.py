"""Regime engine tests: hand-built candles for exact boundaries, plus the fixture scenarios."""
import datetime
import json
import unittest
from decimal import Decimal as Dec

from nifty_core.data.models import LABEL_FIXTURE, LABEL_RECORDED, Candle
from nifty_core.market.ist import FixedClock, IST, UTC, parse_ist
from nifty_core.regime import engine as E
from nifty_core.regime import params as P
from nifty_core.regime.engine import (BEARISH, BULLISH, HIGH_VOLATILITY, NO_DATA, NO_TRADE, SIDEWAYS)
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase

D = datetime.date
DAY = D(2026, 9, 16)
OPEN = datetime.datetime(2026, 9, 16, 9, 15, tzinfo=IST)
DEFAULT = P.load_params()


def params(**override):
    v = {f: getattr(DEFAULT, f) for f in P.FIELDS}
    v.update(override)
    return P.build_params(v)


def session():
    return sc.calendar().session(DAY)


def sess(scenario):
    """The calendar session on a fixture scenario's own date."""
    return sc.calendar().session(scenario.day)


def when(scenario, hhmm):
    """An IST moment on a fixture scenario's own date."""
    return parse_ist("%s %s" % (scenario.day.isoformat(), hhmm))


def candle(i, o, h, l, c, minutes=5, start=None):
    return Candle(start or OPEN + datetime.timedelta(minutes=5 * i), minutes, o, h, l, c)


def flat(n, price="25000.00", first=0):
    return [candle(first + i, price, price, price, price) for i in range(n)]


def classify(candles, as_of=None, p=None, **kw):
    if as_of is None:
        as_of = candles[-1].end if candles else OPEN
    return E.classify(candles, as_of, p or DEFAULT, kw.pop("session", session()), **kw)


def vote(result, name):
    return {e.name: e.vote for e in result.evidence}[name]


def codes(result):
    return [r.code for r in result.reasons]


def set_last(cs, o, h, l, c):
    cs = list(cs)
    cs[-1] = candle(len(cs) - 1, o, h, l, c)
    return cs


class NetMoveBoundaryTests(NoNetworkTestCase):
    """net move = (last close / day open - 1) x 100. Threshold 0.30 is inclusive: 25000 -> 25075.00 is exactly +0.30%."""

    def test_bull_vote_at_exactly_plus_threshold_and_not_below(self):
        base = flat(11)
        self.assertEqual(vote(classify(base + [candle(11, "25000", "25075.00", "25000", "25075.00")]), "net_move"), E.BULL)
        self.assertEqual(vote(classify(base + [candle(11, "25000", "25074.95", "25000", "25074.95")]), "net_move"), E.NEUTRAL)

    def test_bear_vote_at_exactly_minus_threshold_and_not_above(self):
        base = flat(11)
        self.assertEqual(vote(classify(base + [candle(11, "25000", "25000", "24925.00", "24925.00")]), "net_move"), E.BEAR)
        self.assertEqual(vote(classify(base + [candle(11, "25000", "25000", "24925.05", "24925.05")]), "net_move"), E.NEUTRAL)

    def test_the_threshold_is_configurable(self):
        c = flat(11) + [candle(11, "25000", "25050.00", "25000", "25050.00")]            # +0.20%
        self.assertEqual(vote(classify(c), "net_move"), E.NEUTRAL)
        self.assertEqual(vote(classify(c, p=params(net_move_pct=Dec("0.20"))), "net_move"), E.BULL)


class RangePositionBoundaryTests(NoNetworkTestCase):
    """position = (last close - day low) / (day high - day low). Day range here is 24900 to 25100 (200 points)."""

    def series(self, last_close):
        first = candle(0, "25000", "25100", "24900", "25000")
        last_hi, last_lo = max(Dec("25000"), Dec(last_close)), min(Dec("25000"), Dec(last_close))
        return [first] + flat(10, first=1) + [candle(11, "25000", str(last_hi), str(last_lo), last_close)]

    def test_bull_at_exactly_0_65_and_not_below(self):
        self.assertEqual(vote(classify(self.series("25030.00")), "range_position"), E.BULL)       # 130 / 200 = 0.65
        self.assertEqual(vote(classify(self.series("25029.95")), "range_position"), E.NEUTRAL)

    def test_bear_at_exactly_0_35_and_not_above(self):
        self.assertEqual(vote(classify(self.series("24970.00")), "range_position"), E.BEAR)       # 70 / 200 = 0.35
        self.assertEqual(vote(classify(self.series("24970.05")), "range_position"), E.NEUTRAL)

    def test_zero_range_is_neutral_not_an_error(self):
        r = classify(flat(12))
        self.assertEqual(vote(r, "range_position"), E.NEUTRAL)
        self.assertIsNone(r.metrics["range_position"])

    def test_thresholds_are_configurable(self):
        r = classify(self.series("25030.00"), p=params(range_position_high=Dec("0.66")))
        self.assertEqual(vote(r, "range_position"), E.NEUTRAL)


class RecentCandlesBoundaryTests(NoNetworkTestCase):
    UP = ("25000.00", "25000.10", "25000.00", "25000.10")
    DOWN = ("25000.10", "25000.10", "25000.00", "25000.00")
    DOJI = ("25000.00", "25000.10", "25000.00", "25000.00")

    def last6(self, pattern):
        cs = flat(6)
        for i, kind in enumerate(pattern):
            cs.append(candle(6 + i, *getattr(self, kind)))
        return cs

    def test_four_of_six_up_is_bull_three_is_not(self):
        self.assertEqual(vote(classify(self.last6(["UP"] * 4 + ["DOWN"] * 2)), "recent_candles"), E.BULL)
        self.assertEqual(vote(classify(self.last6(["UP"] * 3 + ["DOWN"] * 3)), "recent_candles"), E.NEUTRAL)
        self.assertEqual(vote(classify(self.last6(["UP"] * 3 + ["DOWN"] * 2 + ["DOJI"])), "recent_candles"), E.NEUTRAL)
        self.assertEqual(vote(classify(self.last6(["UP"] * 4 + ["DOWN"] + ["DOJI"])), "recent_candles"), E.BULL)

    def test_four_of_six_down_is_bear_three_is_not(self):
        self.assertEqual(vote(classify(self.last6(["DOWN"] * 4 + ["UP"] * 2)), "recent_candles"), E.BEAR)
        self.assertEqual(vote(classify(self.last6(["DOWN"] * 3 + ["UP"] * 2 + ["DOJI"])), "recent_candles"), E.NEUTRAL)

    def test_only_the_last_window_counts(self):
        cs = [candle(i, *self.UP) for i in range(6)] + [candle(6 + i, *self.DOWN) for i in range(6)]
        self.assertEqual(vote(classify(cs), "recent_candles"), E.BEAR)                              # earlier ups are ignored

    def test_window_and_dominance_are_configurable(self):
        cs = self.last6(["UP"] * 3 + ["DOWN"] * 3)                      # last four candles: UP, DOWN, DOWN, DOWN
        self.assertEqual(vote(classify(cs), "recent_candles"), E.NEUTRAL)
        r = classify(cs, p=params(recent_window=4, recent_dominance=3))
        self.assertEqual(vote(r, "recent_candles"), E.BEAR)


class StructureBoundaryTests(NoNetworkTestCase):
    """Three blocks of 4 candles. A block's high and low are the extremes of its candles."""

    def blocks(self, specs):
        cs = []
        for b, (hi, lo) in enumerate(specs):
            for j in range(4):
                mid = str((Dec(hi) + Dec(lo)) / 2)
                cs.append(candle(4 * b + j, mid, hi, lo, mid))
        return cs

    def test_rising_highs_and_lows_vote_bull(self):
        r = classify(self.blocks([("25010", "24990"), ("25020", "25000"), ("25030", "25010")]))
        self.assertEqual(vote(r, "swing_structure"), E.BULL)

    def test_falling_highs_and_lows_vote_bear(self):
        r = classify(self.blocks([("25030", "25010"), ("25020", "25000"), ("25010", "24990")]))
        self.assertEqual(vote(r, "swing_structure"), E.BEAR)

    def test_an_equal_high_or_low_breaks_the_pattern(self):
        self.assertEqual(vote(classify(self.blocks([("25010", "24990"), ("25010", "25000"), ("25030", "25010")])),
                              "swing_structure"), E.NEUTRAL)
        self.assertEqual(vote(classify(self.blocks([("25010", "24990"), ("25020", "24990"), ("25030", "25010")])),
                              "swing_structure"), E.NEUTRAL)
        self.assertEqual(vote(classify(self.blocks([("25030", "25010"), ("25020", "25010"), ("25010", "24990")])),
                              "swing_structure"), E.NEUTRAL)

    def test_highs_up_but_lows_down_is_neutral(self):
        r = classify(self.blocks([("25010", "25000"), ("25020", "24990"), ("25030", "24980")]))
        self.assertEqual(vote(r, "swing_structure"), E.NEUTRAL)

    def test_only_the_last_structure_window_counts(self):
        early_fall = self.blocks([("25030", "25010"), ("25020", "25000"), ("25010", "24990")])
        rising = self.blocks([("25010", "24990"), ("25020", "25000"), ("25030", "25010")])
        shifted = [candle(12 + i, c.open, c.high, c.low, c.close) for i, c in enumerate(rising)]
        r = classify(early_fall + shifted)
        self.assertEqual(vote(r, "swing_structure"), E.BULL)                  # the earlier falling hour is ignored


class VotesAndDirectionTests(NoNetworkTestCase):
    def rising_tiny(self):
        # a gentle rise: every candle up, highs and lows rising, but the whole day moves only about 0.09%
        return [candle(i, str(Dec("25000") + 2 * i), str(Dec("25001.5") + 2 * i), str(Dec("24999.5") + 2 * i),
                       str(Dec("25001.5") + 2 * i)) for i in range(12)]

    def test_scale_free_evidence_cannot_make_a_direction_out_of_a_tiny_move(self):
        r = classify(self.rising_tiny())
        self.assertEqual(r.bull_votes, 3)                               # position, recent candles, structure all vote bull
        self.assertEqual(vote(r, "net_move"), E.NEUTRAL)                # but the move is only about 0.09%
        self.assertNotEqual(r.label, BULLISH)
        self.assertEqual(r.label, SIDEWAYS)

    def test_mirror_image_for_bearish(self):
        mirrored = [candle(i, str(Dec("25000") - 2 * i), str(Dec("25000.5") - 2 * i), str(Dec("24998.5") - 2 * i),
                           str(Dec("24998.5") - 2 * i)) for i in range(12)]
        r = classify(mirrored)
        self.assertEqual(r.bear_votes, 3)
        self.assertNotEqual(r.label, BEARISH)

    def test_votes_required_boundary_on_a_fixture_moment(self):
        s = sc.scenario("bullish_trend")
        target = None
        for c in s.candles[11:]:
            r = E.classify(s.candles, c.end, DEFAULT, sess(s))
            if r.label == BULLISH and r.bull_votes == 3 and r.bear_votes == 0:
                target = c.end
                break
        self.assertIsNotNone(target, "the bullish fixture should have a moment with exactly 3 bull votes")
        self.assertEqual(E.classify(s.candles, target, DEFAULT, sess(s)).label, BULLISH)
        strict = E.classify(s.candles, target, params(votes_required=4), sess(s))
        self.assertEqual(strict.label, NO_TRADE)
        self.assertEqual(codes(strict), ["INSUFFICIENT_EVIDENCE"])

    def test_one_vote_against_blocks_a_direction(self):
        s = sc.scenario("bearish_trend")
        found = False
        for c in s.candles[11:]:
            r = E.classify(s.candles, c.end, DEFAULT, sess(s))
            if r.bull_votes >= 1 and r.bear_votes >= 1 and r.metrics["range_pct"] > DEFAULT.sideways_max_range_pct                     and r.label != HIGH_VOLATILITY:
                self.assertEqual(r.label, NO_TRADE)
                self.assertEqual(codes(r), ["CONFLICTING_EVIDENCE"])
                found = True
        self.assertTrue(found)

    def test_score_subtracts_bear_votes_from_bull_votes(self):
        mirrored = [candle(i, str(Dec("25000") - 2 * i), str(Dec("25000.5") - 2 * i), str(Dec("24998.5") - 2 * i),
                           str(Dec("24998.5") - 2 * i)) for i in range(12)]
        self.assertEqual(classify(mirrored).evidence_score, Dec(-3) / Dec(4))             # three bear votes
        s = sc.scenario("bearish_trend")
        seen = set()
        for c in s.candles[11:]:
            r = E.classify(s.candles, c.end, DEFAULT, sess(s))
            self.assertEqual(r.evidence_score, Dec(r.bull_votes - r.bear_votes) / Dec(4))
            seen.add((r.bull_votes > 0, r.bear_votes > 0))
        self.assertIn((True, True), seen)                                                   # mixed votes were exercised
        self.assertIn((False, True), seen)
        end = E.classify(s.candles, s.candles[-1].end, DEFAULT, sess(s))
        self.assertEqual(end.evidence_score, Dec(-1))                                       # all four vote bear

    def test_score_is_votes_over_four_and_is_not_a_probability(self):
        r = classify(self.rising_tiny())
        self.assertEqual(r.evidence_score, Dec(3) / Dec(4))
        self.assertIn("not a probability", r.note)
        self.assertIn("Not a prediction", r.note)


class SidewaysBoundaryTests(NoNetworkTestCase):
    def with_extremes(self, high, low, last="25000.00"):
        first = candle(0, "25000", high, low, "25000")
        return [first] + flat(10, first=1) + [candle(11, "25000", max(Dec(last), Dec("25000")), min(Dec(last), Dec("25000")), last)]

    def test_range_exactly_at_the_limit_is_sideways_just_above_is_not(self):
        # 0.45% of 25000 = 112.50 points
        self.assertEqual(classify(self.with_extremes("25056.25", "24943.75")).label, SIDEWAYS)
        r = classify(self.with_extremes("25056.30", "24943.75"))                      # 112.55 -> 0.4502%
        self.assertNotEqual(r.label, SIDEWAYS)
        self.assertEqual(r.label, NO_TRADE)
        self.assertEqual(codes(r), ["INSUFFICIENT_EVIDENCE"])

    def test_net_move_must_be_strictly_inside_the_directional_threshold(self):
        base = flat(11)
        just_in = base + [candle(11, "25000", "25074.95", "25000", "25074.95")]       # +0.29998%
        exactly = base + [candle(11, "25000", "25075.00", "25000", "25075.00")]       # +0.30%
        self.assertEqual(classify(just_in).label, SIDEWAYS)
        r = classify(exactly)
        self.assertNotEqual(r.label, SIDEWAYS)
        self.assertEqual(r.label, NO_TRADE)                                           # one directional vote is not enough

    def test_range_limit_is_configurable(self):
        c = self.with_extremes("25056.30", "24943.75")
        self.assertEqual(classify(c, p=params(sideways_max_range_pct=Dec("0.46"))).label, SIDEWAYS)


class HighVolatilityBoundaryTests(NoNetworkTestCase):
    def one_big_bar(self, high, low):
        return [candle(0, "25000", high, low, "25000")] + flat(11, first=1)

    def test_a_bar_range_of_exactly_the_limit_is_high_volatility_just_below_is_not(self):
        r = classify(self.one_big_bar("25062.50", "24937.50"))                        # 125.00 / 25000 = 0.50%
        self.assertEqual(r.label, HIGH_VOLATILITY)
        self.assertEqual(codes(r), ["ABNORMAL_BAR_RANGE"])
        r = classify(self.one_big_bar("25062.45", "24937.50"))                        # 124.95 -> 0.4998%
        self.assertNotEqual(r.label, HIGH_VOLATILITY)

    def test_a_single_huge_bar_is_enough_even_when_everything_else_is_calm(self):
        self.assertEqual(classify(self.one_big_bar("25400.00", "24600.00")).label, HIGH_VOLATILITY)

    def test_realised_volatility_boundary_is_inclusive(self):
        s = sc.scenario("bearish_trend")
        r = E.classify(s.candles, s.candles[40].end, DEFAULT, sess(s))
        sd = r.metrics["realised_stdev_pct"]
        self.assertNotEqual(r.label, HIGH_VOLATILITY)
        exactly = E.classify(s.candles, s.candles[40].end, params(high_vol_stdev_pct=sd), sess(s))
        self.assertEqual(exactly.label, HIGH_VOLATILITY)
        self.assertEqual(codes(exactly), ["HIGH_REALISED_VOLATILITY"])
        above = E.classify(s.candles, s.candles[40].end, params(high_vol_stdev_pct=sd + Dec("0.0000000001")), sess(s))
        self.assertNotEqual(above.label, HIGH_VOLATILITY)

    def test_volatility_takes_precedence_over_a_direction(self):
        rising = VotesAndDirectionTests().rising_tiny()
        rising[5] = candle(5, "25010", "25100.00", "24970.00", "25011.5")             # one bar of about 0.52%
        r = classify(rising)
        self.assertEqual(r.label, HIGH_VOLATILITY)
        self.assertGreaterEqual(r.bull_votes, 1)                                      # the evidence is still shown

    def test_both_volatility_reasons_are_reported_when_both_apply(self):
        s = sc.scenario("high_volatility")
        r = E.classify(s.candles, s.candles[-1].end, DEFAULT, sess(s))
        self.assertEqual(codes(r), ["ABNORMAL_BAR_RANGE", "HIGH_REALISED_VOLATILITY"])


class DataGateTests(NoNetworkTestCase):
    def good(self, n=12):
        return list(sc.scenario("sideways").candles[:n])

    def test_exactly_min_bars_is_enough_one_fewer_is_not(self):
        s = sc.scenario("sideways")
        ok = E.classify(s.candles[:12], s.candles[11].end, DEFAULT, sess(s))
        self.assertEqual(ok.label, SIDEWAYS)
        short = E.classify(s.candles[:11], s.candles[10].end, DEFAULT, sess(s))
        self.assertEqual(short.label, NO_DATA)
        self.assertEqual(codes(short), ["INSUFFICIENT_HISTORY"])
        self.assertEqual(E.classify(s.candles[:11], s.candles[10].end, params(min_bars=12), sess(s)).label, NO_DATA)

    def test_min_bars_is_configurable_but_cannot_go_below_the_windows(self):
        from nifty_core.errors import ConfigError
        with self.assertRaises(ConfigError):
            params(min_bars=6)

    def test_data_lag_boundary(self):
        s = sc.scenario("sideways")
        cs = s.candles[:12]                                                            # last candle ends 10:15
        self.assertEqual(E.classify(cs, when(s, "10:25"), DEFAULT, sess(s)).label, SIDEWAYS)   # 10 min: allowed
        late = E.classify(cs, when(s, "10:25:01"), DEFAULT, sess(s))                            # 10 min 1 s: stale
        self.assertEqual(late.label, NO_DATA)
        self.assertEqual(codes(late), ["STALE_DATA"])

    def test_after_the_close_a_complete_day_is_not_stale(self):
        s = sc.scenario("sideways")
        r = E.classify(s.candles, when(s, "18:00"), DEFAULT, sess(s))
        self.assertEqual(r.label, SIDEWAYS)

    def test_no_candles_and_only_future_candles(self):
        r = E.classify([], parse_ist("2026-09-16 11:00"), DEFAULT, session())
        self.assertEqual((r.label, codes(r)), (NO_DATA, ["NO_CANDLES"]))
        s = sc.scenario("sideways")
        r = E.classify(s.candles, when(s, "09:15"), DEFAULT, sess(s))
        self.assertEqual((r.label, codes(r)), (NO_DATA, ["NO_CANDLES"]))
        self.assertEqual(r.ignored_future_candles, 77)

    def test_a_hole_in_the_day_blocks_everything_after_it(self):
        s = sc.scenario("sideways")
        holed = s.candles[:14] + s.candles[15:30]
        r = E.classify(holed, holed[-1].end, DEFAULT, sess(s))
        self.assertEqual(r.label, NO_DATA)
        self.assertIn("GAP_IN_CANDLES", codes(r))
        before = E.classify(holed, holed[13].end, DEFAULT, sess(s))
        self.assertEqual(before.label, SIDEWAYS)                                       # the history up to the hole is fine

    def test_data_that_does_not_start_at_the_open(self):
        s = sc.scenario("sideways")
        r = E.classify(s.candles[1:14], s.candles[13].end, DEFAULT, sess(s))
        self.assertEqual(r.label, NO_DATA)
        self.assertIn("LATE_START", codes(r))

    def test_wrong_interval_overlap_other_day_and_outside_session(self):
        base = flat(12)
        one_min = [candle(i, "25000", "25000", "25000", "25000", minutes=1) for i in range(12)]
        self.assertIn("WRONG_INTERVAL", codes(classify(one_min, as_of=parse_ist("2026-09-16 10:15"))))
        dup = base + [base[11]]
        self.assertIn("OUT_OF_ORDER", codes(classify(dup, as_of=parse_ist("2026-09-16 10:15"))))
        shifted = [candle(0, "25000", "25000", "25000", "25000", start=OPEN - datetime.timedelta(days=1))] + base[1:]
        self.assertIn("DAY_MISMATCH", codes(classify(shifted, as_of=parse_ist("2026-09-16 10:15"))))
        late = [candle(0, "25000", "25000", "25000", "25000", start=parse_ist("2026-09-16 15:40"))]
        self.assertIn("OUTSIDE_SESSION", codes(classify(late, as_of=parse_ist("2026-09-16 15:50"))))

    def test_closed_market_and_wrong_day(self):
        r = E.classify([], parse_ist("2026-11-10 11:00"), DEFAULT, None)                # a holiday: no session
        self.assertEqual((r.label, codes(r)), (NO_DATA, ["MARKET_CLOSED"]))
        r = E.classify(sc.scenario("sideways").candles, parse_ist("2026-09-17 11:00"), DEFAULT, session())
        self.assertEqual((r.label, codes(r)), (NO_DATA, ["DAY_MISMATCH"]))

    def test_non_candle_input_is_refused_explicitly(self):
        r = E.classify([{"open": 1}], parse_ist("2026-09-16 10:15"), DEFAULT, session())
        self.assertEqual((r.label, codes(r)), (NO_DATA, ["INVALID_CANDLE"]))

    def test_naive_as_of_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            E.classify([], datetime.datetime(2026, 9, 16, 10, 15), DEFAULT, session())

    def test_unverified_source_blocks_in_a_real_mode(self):
        s = sc.scenario("sideways")
        for label in (None, LABEL_FIXTURE):
            r = E.classify(s.candles, s.candles[-1].end, DEFAULT, sess(s), data_mode="real_readonly", data_label=label)
            self.assertEqual(r.label, NO_DATA)
            self.assertIn("UNVERIFIED_SOURCE", codes(r))
        ok = E.classify(s.candles, s.candles[-1].end, DEFAULT, sess(s), data_mode="real_readonly",
                        data_label=LABEL_RECORDED)
        self.assertEqual(ok.label, SIDEWAYS)

    def test_no_data_never_carries_a_direction(self):
        r = E.classify([], parse_ist("2026-09-16 11:00"), DEFAULT, session())
        self.assertEqual((r.bull_votes, r.bear_votes, r.evidence, r.evidence_score), (0, 0, (), None))
        self.assertTrue(r.data_issues)

    def test_all_problems_are_listed_not_just_the_first(self):
        s = sc.scenario("sideways")
        r = E.classify(s.candles[1:5], when(s, "12:00"), DEFAULT, sess(s))
        for code in ("LATE_START", "INSUFFICIENT_HISTORY", "STALE_DATA"):
            self.assertIn(code, codes(r))


class FixtureClassificationTests(NoNetworkTestCase):
    """Labels on the committed synthetic fixtures. They show the logic works, not that it works on real markets."""

    def label_at(self, name, hhmm):
        s = sc.scenario(name)
        h, m = hhmm.split(":")
        t = datetime.datetime(s.day.year, s.day.month, s.day.day, int(h), int(m), tzinfo=IST)
        return E.classify_at(sc.provider(name), sc.calendar(), t, DEFAULT, data_label=s.data_label)

    def test_bullish_fixture(self):
        for t in ("11:00", "12:00", "13:00"):
            r = self.label_at("bullish_trend", t)
            self.assertEqual(r.label, BULLISH, t)
            self.assertEqual(vote(r, "net_move"), E.BULL)
            self.assertEqual(r.bear_votes, 0)

    def test_bullish_fixture_never_turns_bearish(self):
        s = sc.scenario("bullish_trend")
        labels = {E.classify(s.candles, c.end, DEFAULT, sess(s)).label for c in s.candles}
        self.assertNotIn(BEARISH, labels)
        self.assertIn(BULLISH, labels)

    def test_bearish_fixture(self):
        for t in ("14:30", "15:00", "15:30"):
            self.assertEqual(self.label_at("bearish_trend", t).label, BEARISH, t)

    def test_sideways_fixture(self):
        s = sc.scenario("sideways")
        labels = {E.classify(s.candles, c.end, DEFAULT, sess(s)).label for c in s.candles[11:]}
        self.assertEqual(labels, {SIDEWAYS})

    def test_calm_illiquid_option_day_is_a_sideways_index_regime(self):
        # the regime looks at the index only; option liquidity is a different gate
        s = sc.scenario("illiquid_options")
        labels = {E.classify(s.candles, c.end, DEFAULT, sess(s)).label for c in s.candles[11:]}
        self.assertEqual(labels, {SIDEWAYS})

    def test_high_volatility_fixture_is_high_volatility_all_day_and_never_directional(self):
        s = sc.scenario("high_volatility")
        labels = {E.classify(s.candles, c.end, DEFAULT, sess(s)).label for c in s.candles[11:]}
        self.assertEqual(labels, {HIGH_VOLATILITY})
        before_jump = E.classify(s.candles, s.candles[35].end, DEFAULT, sess(s))      # the jump is bar 36
        self.assertEqual(codes(before_jump), ["HIGH_REALISED_VOLATILITY"])
        after_jump = E.classify(s.candles, s.candles[36].end, DEFAULT, sess(s))
        self.assertIn("ABNORMAL_BAR_RANGE", codes(after_jump))

    def test_missing_and_invalid_data_fixture_is_never_classified(self):
        s = sc.scenario("missing_invalid_data")
        labels = {E.classify_at(sc.provider("missing_invalid_data"), sc.calendar(),
                                datetime.datetime(2026, 9, 30, h, m, tzinfo=IST), DEFAULT, data_label=s.data_label).label
                  for h in range(9, 16) for m in range(0, 60, 5)}
        self.assertEqual(labels, {NO_DATA})
        after_gap = E.classify(s.candles, s.candles[-1].end, DEFAULT, sc.calendar().session(s.day))
        self.assertEqual(codes(after_gap), ["GAP_IN_CANDLES"])

    def test_nothing_is_classified_before_min_bars(self):
        for name in ("bullish_trend", "bearish_trend", "sideways", "high_volatility"):
            r = self.label_at(name, "10:10")
            self.assertEqual((r.label, codes(r)), (NO_DATA, ["INSUFFICIENT_HISTORY"]), name)
            self.assertNotEqual(self.label_at(name, "10:15").label, NO_DATA, name)

    def test_the_known_weak_spots_are_visible_not_hidden(self):
        # on the bearish day the market first rose above its open, and the engine said so at the time
        s = sc.scenario("bearish_trend")
        early = {E.classify(s.candles, c.end, DEFAULT, sess(s)).label for c in s.candles[11:40]}
        self.assertIn(BULLISH, early)
        self.assertEqual(self.label_at("bearish_trend", "15:30").label, BEARISH)

    def test_classify_at_equals_classify_with_the_same_candles(self):
        s = sc.scenario("bullish_trend")
        t = parse_ist("2026-09-16 12:00")
        a = E.classify_at(sc.provider("bullish_trend"), sc.calendar(), t, DEFAULT, data_label=s.data_label)
        b = E.classify([c for c in s.candles if c.end <= t], t, DEFAULT, sess(s), data_label=s.data_label)
        self.assertEqual(a.to_journal_dict(), b.to_journal_dict())

    def test_classify_at_on_a_closed_day_and_with_a_wrong_interval(self):
        r = E.classify_at(sc.provider("sideways"), sc.calendar(), parse_ist("2026-11-10 11:00"), DEFAULT)
        self.assertEqual((r.label, codes(r)), (NO_DATA, ["MARKET_CLOSED"]))
        p1 = params()
        p1 = p1._replace(interval_minutes=1)                                          # bypass validation on purpose
        r = E.classify_at(sc.provider("sideways"), sc.calendar(), parse_ist("2026-09-23 11:00"), p1)
        self.assertEqual((r.label, codes(r)), (NO_DATA, ["PROVIDER_UNAVAILABLE"]))


class NoFutureLeakageTests(NoNetworkTestCase):
    def comparable(self, result):
        d = result.to_journal_dict()
        d.pop("ignored_future_candles")
        return d

    def test_a_result_never_changes_when_later_candles_are_added_or_removed(self):
        for name in ("bullish_trend", "bearish_trend", "sideways", "high_volatility"):
            s = sc.scenario(name)
            for c in s.candles:
                full = E.classify(s.candles, c.end, DEFAULT, sess(s))
                cut = E.classify([x for x in s.candles if x.end <= c.end], c.end, DEFAULT, sess(s))
                self.assertEqual(self.comparable(full), self.comparable(cut), (name, c.end))
                self.assertEqual(full.ignored_future_candles, len([x for x in s.candles if x.end > c.end]))

    def test_corrupting_future_candles_changes_nothing(self):
        s = sc.scenario("bullish_trend")
        at = s.candles[40].end
        honest = E.classify(s.candles, at, DEFAULT, sess(s))
        future = [Candle(c.start, 5, "1.00", "99999.00", "1.00", "99999.00") if c.start >= at else c for c in s.candles]
        self.assertEqual(self.comparable(honest), self.comparable(E.classify(future, at, DEFAULT, session())))

    def test_a_candle_still_forming_is_not_used(self):
        s = sc.scenario("bullish_trend")
        done = E.classify(s.candles, s.candles[30].end, DEFAULT, sess(s))
        mid_candle = s.candles[30].end + datetime.timedelta(minutes=2)                # 2 minutes into the next candle
        during = E.classify(s.candles, mid_candle, DEFAULT, sess(s))
        self.assertEqual(during.candles_used, done.candles_used)
        self.assertEqual(during.metrics, done.metrics)
        self.assertEqual(during.label, done.label)

    def test_the_provider_path_cannot_see_the_future_either(self):
        s = sc.scenario("high_volatility")
        for c in s.candles[11:]:
            via_provider = E.classify_at(sc.provider("high_volatility"), sc.calendar(), c.end, DEFAULT,
                                         data_label=s.data_label)
            self.assertEqual(via_provider.candles_used, len([x for x in s.candles if x.end <= c.end]))
            self.assertEqual(via_provider.ignored_future_candles, 0)

    def test_the_engine_reads_no_clock_and_no_options_data(self):
        import inspect
        src = inspect.getsource(E)
        for banned in ("datetime.now(", "utcnow", ".today(", "OptionChain", "OptionQuote", "get_option_chain"):
            self.assertNotIn(banned, src, banned)


class DeterminismAndExplanationTests(NoNetworkTestCase):
    def test_same_instant_in_any_time_zone_gives_the_same_result(self):
        s = sc.scenario("bullish_trend")
        ist = parse_ist("2026-09-16 12:00")
        utc = ist.astimezone(UTC)
        ny = ist.astimezone(datetime.timezone(datetime.timedelta(hours=-4)))
        results = [E.classify(s.candles, t, DEFAULT, sess(s)).to_journal_dict() for t in (ist, utc, ny)]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]["as_of"], results[2]["as_of"])
        self.assertEqual(results[0], results[2])
        self.assertTrue(results[0]["as_of"].endswith("+05:30"))

    def test_repeated_runs_are_identical(self):
        s = sc.scenario("bearish_trend")
        t = parse_ist("2026-09-17 13:00")
        a = E.classify(s.candles, t, DEFAULT, sc.calendar().session(s.day)).to_journal_dict()
        b = E.classify(s.candles, t, DEFAULT, sc.calendar().session(s.day)).to_journal_dict()
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_every_classification_explains_itself(self):
        for name in ("bullish_trend", "bearish_trend", "sideways", "high_volatility"):
            s = sc.scenario(name)
            for c in s.candles[11::6]:
                d = E.classify(s.candles, c.end, DEFAULT, sc.calendar().session(s.day)).to_journal_dict()
                self.assertIn(d["label"], E.LABELS)
                self.assertTrue(d["reasons"], d["label"])
                self.assertEqual([e["name"] for e in d["evidence"]],
                                 ["net_move", "range_position", "recent_candles", "swing_structure"])
                for e in d["evidence"]:
                    self.assertIn(e["vote"], ("BULL", "BEAR", "NEUTRAL"))
                    self.assertTrue(e["observed"] and e["rule"])
                for key in ("net_move_pct", "range_pct", "realised_stdev_pct", "max_bar_range_pct", "day_open"):
                    self.assertIn(key, d["metrics"])
                self.assertEqual(d["thresholds_fingerprint"], DEFAULT.fingerprint)
                self.assertEqual(d["bull_votes"], sum(1 for e in d["evidence"] if e["vote"] == "BULL"))
                json.dumps(d)                                                           # plain JSON-able

    def test_rejections_explain_themselves_too(self):
        d = E.classify([], parse_ist("2026-09-16 11:00"), DEFAULT, session()).to_journal_dict()
        self.assertEqual(d["label"], "NO_DATA")
        self.assertEqual(d["data_issues"][0]["code"], "NO_CANDLES")
        self.assertEqual(d["evidence"], [])

    def test_changing_a_threshold_is_visible_in_the_record(self):
        s = sc.scenario("bullish_trend")
        a = E.classify(s.candles, s.candles[40].end, DEFAULT, sess(s))
        b = E.classify(s.candles, s.candles[40].end, params(net_move_pct=Dec("0.31")), sess(s))
        self.assertNotEqual(a.thresholds_fingerprint, b.thresholds_fingerprint)

    def test_label_set_is_the_documented_one(self):
        self.assertEqual(set(E.LABELS), {"BULLISH", "BEARISH", "SIDEWAYS", "HIGH_VOLATILITY", "NO_DATA", "NO_TRADE"})


if __name__ == "__main__":
    unittest.main()
