"""The intraday setup (opening-range breakout entered on a retest): each rule has a test that breaks only that rule."""
import datetime
import unittest
from decimal import Decimal as D

from nifty_core.data.models import Candle
from nifty_core.errors import ConfigError
from nifty_core.intraday import params as P
from nifty_core.intraday import setup as S
from tests._base import NoNetworkTestCase

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
OPEN = datetime.datetime(2026, 11, 5, 9, 15, tzinfo=IST)
VALUES = {"opening_range_candles": 6, "min_range_pct": D("0.10"), "breakout_buffer_pct": D("0.03"),
          "max_extension_pct": D("0.20"), "require_retest": True, "retest_zone_pct": D("0.05"),
          "max_breakout_age_candles": 12, "require_resumption_candle": True, "require_day_average_side": True,
          "latest_setup_ist": "13:30"}
PARAMS = P.build_params(VALUES)

# opening range: high 24400, low 24300 (0.41% wide); buffer level 24407.32; retest zone up to 24412.2
OPENING = [(24350, 24380, 24320, 24360), (24360, 24370, 24300, 24310), (24310, 24340, 24305, 24335),
           (24335, 24390, 24330, 24385), (24385, 24400, 24370, 24380), (24380, 24395, 24372, 24390)]
BREAKOUT = (24390, 24425, 24388, 24420)      # first close above 24407.32
PULLBACK = (24420, 24428, 24402, 24410)      # low <= 24412.2 (a retest); still closed above 24400; a red candle
RESUME = (24410, 24425, 24408, 24422)        # green, 0.09% beyond the level


def candles(rows):
    return [Candle(OPEN + datetime.timedelta(minutes=5 * i), 5, *[D(str(x)) for x in row]) for i, row in enumerate(rows)]


def end_of(cs):
    return cs[-1].end


def mirror(rows, pivot=24350):
    """The bearish twin of a bullish day: every price reflected around the pivot (highs and lows swap)."""
    return [(2 * pivot - o, 2 * pivot - l, 2 * pivot - h, 2 * pivot - c) for o, h, l, c in rows]


def run(rows, params=PARAMS):
    cs = candles(rows)
    return S.evaluate(cs, end_of(cs), params)


def codes(result):
    return [r.code for r in result.reasons]


class BullishSetupTests(NoNetworkTestCase):
    def test_breakout_then_pullback_then_a_resuming_candle_is_a_bull_setup(self):
        r = run(OPENING + [BREAKOUT, PULLBACK, RESUME])
        self.assertEqual((r.direction, codes(r)), (S.BULL, ["INTRADAY_SETUP"]))
        self.assertIn("pulled back to it at 09:50", r.reasons[0].text)
        self.assertEqual(r.metrics["opening_range_high"], D(24400))
        self.assertEqual(r.metrics["opening_range_low"], D(24300))

    def test_no_entry_on_the_breakout_candle_itself_waiting_for_the_pullback(self):
        r = run(OPENING + [BREAKOUT])
        self.assertIsNone(r.direction)
        self.assertEqual(codes(r)[0], "SETUP_NO_RETEST")

    def test_no_pullback_means_no_setup(self):
        shallow = (24420, 24430, 24416, 24424)                        # low stays above the retest zone
        r = run(OPENING + [BREAKOUT, shallow, (24424, 24432, 24420, 24430)])
        self.assertIsNone(r.direction)
        self.assertIn("SETUP_NO_RETEST", codes(r))

    def test_a_close_back_inside_the_range_cancels_the_breakout(self):
        r = run(OPENING + [BREAKOUT, (24420, 24421, 24385, 24392), RESUME])
        self.assertIsNone(r.direction)                                # RESUME closes at 24422 but 24407.32 is not re-cleared
        r2 = run(OPENING + [BREAKOUT, (24420, 24421, 24385, 24392)])
        self.assertEqual(codes(r2), ["SETUP_FAILED_BREAKOUT"])
        self.assertIn("failed", r2.reasons[0].text)

    def test_a_chased_move_is_refused_until_it_comes_back(self):
        far = (24410, 24500, 24408, 24470)                            # 0.29% beyond the level, limit 0.20%
        r = run(OPENING + [BREAKOUT, PULLBACK, far])
        self.assertIsNone(r.direction)
        self.assertIn("SETUP_EXTENDED", codes(r))
        self.assertIn("not chasing", r.reasons[0].text)

    def test_the_resuming_candle_must_close_up(self):
        r = run(OPENING + [BREAKOUT, PULLBACK, (24410, 24415, 24401, 24405)])
        self.assertIsNone(r.direction)
        self.assertEqual(codes(r), ["SETUP_NO_RESUMPTION"])

    def test_the_close_must_be_on_the_right_side_of_the_day_average(self):
        spike = (24390, 24900, 24388, 24880)                          # a huge first breakout pulls the day average up
        r = run(OPENING + [spike, (24880, 24890, 24402, 24410), RESUME])
        self.assertIsNone(r.direction)
        self.assertIn("SETUP_WRONG_SIDE_OF_DAY_AVERAGE", codes(r))
        relaxed = P.build_params(dict(VALUES, require_day_average_side=False, max_extension_pct=D("1")))
        self.assertEqual(run(OPENING + [spike, (24880, 24890, 24402, 24410), RESUME], relaxed).direction, S.BULL)

    def test_a_new_breakout_after_a_failed_one_counts_as_new(self):
        rows = OPENING + [BREAKOUT, (24420, 24421, 24385, 24392), (24392, 24420, 24390, 24418), PULLBACK, RESUME]
        r = run(rows)
        self.assertEqual(r.direction, S.BULL)
        self.assertIn("09:55", r.reasons[0].text)                      # the second breakout, not the first

    def test_an_old_breakout_goes_stale(self):
        drift = [(24422, 24428, 24415, 24420)] * 14
        r = run(OPENING + [BREAKOUT, PULLBACK] + drift)
        self.assertIn("SETUP_BREAKOUT_STALE", codes(r))
        self.assertIsNone(r.direction)

    def test_entering_on_the_breakout_when_a_retest_is_not_required(self):
        loose = P.build_params(dict(VALUES, require_retest=False))
        self.assertEqual(run(OPENING + [BREAKOUT], loose).direction, S.BULL)
        self.assertIsNone(run(OPENING + [BREAKOUT]).direction)


class BearishSetupTests(NoNetworkTestCase):
    def test_the_mirror_image_is_a_bear_setup(self):
        r = run(mirror(OPENING + [BREAKOUT, PULLBACK, RESUME]))
        self.assertEqual((r.direction, codes(r)), (S.BEAR, ["INTRADAY_SETUP"]))

    def test_every_bull_refusal_has_a_bear_twin(self):
        for name, rows, code in (
                ("no retest", OPENING + [BREAKOUT, (24420, 24430, 24416, 24424), (24424, 24432, 24420, 24430)], "SETUP_NO_RETEST"),
                ("chased", OPENING + [BREAKOUT, PULLBACK, (24410, 24500, 24408, 24470)], "SETUP_EXTENDED"),
                ("red resume", OPENING + [BREAKOUT, PULLBACK, (24410, 24415, 24401, 24405)], "SETUP_NO_RESUMPTION")):
            r = run(mirror(rows))
            self.assertIsNone(r.direction, name)
            self.assertIn(code, codes(r), name)


class WhenNotToTradeTests(NoNetworkTestCase):
    def test_the_opening_range_must_be_complete(self):
        r = run(OPENING[:5])
        self.assertEqual(codes(r), ["SETUP_OPENING_RANGE_FORMING"])
        self.assertEqual(codes(run(OPENING)), ["SETUP_NO_BREAKOUT"])    # complete, but price is inside it

    def test_a_tiny_opening_range_is_noise(self):
        tight = [(24350, 24352, 24348, 24351)] * 6
        r = run(tight + [(24351, 24360, 24350, 24358)])
        self.assertEqual(codes(r), ["SETUP_RANGE_TOO_NARROW"])

    def test_no_new_setup_after_the_cut_off_time(self):
        late = P.build_params(dict(VALUES, latest_setup_ist="09:46"))
        r = run(OPENING + [BREAKOUT, PULLBACK, RESUME], late)
        self.assertEqual(codes(r), ["SETUP_TOO_LATE"])

    def test_a_candle_that_has_not_finished_is_never_used(self):
        cs = candles(OPENING + [BREAKOUT, PULLBACK, RESUME])
        early = S.evaluate(cs, cs[-1].start + datetime.timedelta(minutes=2), PARAMS)     # the last candle is still forming
        self.assertIsNone(early.direction)
        self.assertEqual(S.evaluate(cs, cs[-1].end, PARAMS).direction, S.BULL)

    def test_a_naive_time_is_refused(self):
        with self.assertRaises(ValueError):
            S.evaluate(candles(OPENING), datetime.datetime(2026, 11, 5, 10, 0), PARAMS)


class ParamsTests(NoNetworkTestCase):
    def test_bad_values_are_refused(self):
        for key, bad in (("opening_range_candles", 1), ("opening_range_candles", True), ("min_range_pct", -1),
                         ("max_extension_pct", 9), ("require_retest", "yes"), ("latest_setup_ist", "late"),
                         ("max_breakout_age_candles", 0)):
            with self.assertRaises(ConfigError, msg=key):
                P.build_params(dict(VALUES, **{key: bad}))
        with self.assertRaises(ConfigError):
            P.build_params({k: v for k, v in VALUES.items() if k != "min_range_pct"})
        with self.assertRaises(ConfigError):
            P.build_params(dict(VALUES, surprise=1))

    def test_a_change_changes_the_fingerprint(self):
        self.assertNotEqual(PARAMS.fingerprint, P.build_params(dict(VALUES, require_retest=False)).fingerprint)
        self.assertEqual(PARAMS.fingerprint, P.build_params(dict(VALUES)).fingerprint)


class TodaysRealCandlesTests(NoNetworkTestCase):
    """The recorded NIFTY 5-minute candles of 2026-11-05 (09:15-10:30): the setup saw the 09:45 breakout, never got a
    resuming candle after the pullback, and the breakout then failed. No setup, so no trade."""
    ROWS = [(24378.05, 24391.95, 24296.3, 24356.15), (24354.7, 24363.5, 24335.55, 24341.35),
            (24341.85, 24357.2, 24329.2, 24347.4), (24347.55, 24350.0, 24305.35, 24323.85),
            (24323.85, 24371.6, 24323.9, 24358.8), (24358.8, 24391.1, 24358.8, 24386.4),
            (24386.4, 24417.45, 24386.65, 24413.1), (24413.1, 24441.8, 24405.55, 24437.8),
            (24437.8, 24442.2, 24401.55, 24402.5), (24402.5, 24410.6, 24384.6, 24400.15),
            (24400.15, 24402.8, 24365.5, 24373.6), (24373.6, 24377.7, 24360.65, 24366.7)]

    def at(self, n):
        rows = [(o, max(h, o, c), min(l, o, c), c) for o, h, l, c in self.ROWS[:n]]
        cs = candles(rows)
        return S.evaluate(cs, end_of(cs), PARAMS)

    def test_the_day_unfolds_as_waiting_then_no_resumption_then_a_failed_breakout(self):
        self.assertEqual(codes(self.at(6)), ["SETUP_NO_BREAKOUT"])                # 09:45: range just completed
        self.assertEqual(codes(self.at(7))[0], "SETUP_NO_RETEST")                 # 09:50: broke out, no pullback yet
        self.assertIn("SETUP_NO_RETEST", codes(self.at(8)))
        self.assertIn("SETUP_NO_RESUMPTION", codes(self.at(9)))                   # pulled back, but the candle was red
        self.assertIn("SETUP_NO_RESUMPTION", codes(self.at(10)))
        self.assertEqual(codes(self.at(11)), ["SETUP_FAILED_BREAKOUT"])           # 10:05 close back inside the range
        for n in range(6, 13):
            self.assertIsNone(self.at(n).direction)


if __name__ == "__main__":
    unittest.main()
