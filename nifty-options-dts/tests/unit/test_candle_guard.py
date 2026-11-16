"""The candle-finality guard: a decision is only taken on a candle that has stopped changing and agrees with the spot.

The numbers are the real 2026-11-06 cases: at 10:30:05 the vendor's 10:25 candle showed a close near 24,670 while the final
close was 24,697.9 and the spot a few seconds before the boundary was 24,692.65."""
import datetime as dt
import unittest
from decimal import Decimal as D

from nifty_live import candle_guard as G
from tests._base import NoNetworkTestCase

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
SLOT = dt.datetime(2026, 11, 6, 10, 30, tzinfo=IST)           # the slot whose last candle is the 10:25 one


def row(close, start="2026-11-06T10:25:00+05:30"):
    return {"start": start, "open": "24655.8", "high": "24698.25", "low": "24635.65", "close": str(close)}


def at(seconds):
    return SLOT + dt.timedelta(seconds=seconds)


SPOTS = ((SLOT - dt.timedelta(seconds=5), D("24692.65")),)


class GuardTests(NoNetworkTestCase):
    def test_a_missing_candle_waits_then_the_slot_is_skipped(self):
        g = G.CandleGuard()
        self.assertEqual(g.check(SLOT, at(5), [], SPOTS).state, G.WAIT)
        self.assertEqual(g.check(SLOT, at(60), [], SPOTS).state, G.WAIT)
        v = g.check(SLOT, at(125), [], SPOTS)
        self.assertEqual(v.state, G.SKIP)
        self.assertIn("not published", v.text)

    def test_a_candle_must_settle_after_the_boundary_even_if_it_looks_right(self):
        g = G.CandleGuard()
        g.check(SLOT, at(5), [row("24697.9")], SPOTS)
        self.assertEqual(g.check(SLOT, at(15), [row("24697.9")], SPOTS).state, G.WAIT)       # stable, but only 15 s old

    def test_a_candle_that_changes_between_reads_is_not_final(self):
        g = G.CandleGuard()
        g.check(SLOT, at(35), [row("24690.0")], SPOTS)
        v = g.check(SLOT, at(45), [row("24697.9")], SPOTS)
        self.assertEqual((v.state, "changed" in v.text), (G.WAIT, True))
        self.assertEqual(g.check(SLOT, at(55), [row("24697.9")], SPOTS).state, G.GO)

    def test_a_stable_candle_that_agrees_with_the_spot_is_final(self):
        g = G.CandleGuard()
        g.check(SLOT, at(35), [row("24697.9")], SPOTS)
        self.assertEqual(g.check(SLOT, at(45), [row("24697.9")], SPOTS).state, G.GO)

    def test_the_real_provisional_candle_is_held_back_then_accepted_once_the_vendor_has_caught_up(self):
        g = G.CandleGuard()
        g.check(SLOT, at(35), [row("24669.8")], SPOTS)
        v = g.check(SLOT, at(45), [row("24669.8")], SPOTS)            # stable but 0.09% from the spot: still provisional
        self.assertEqual(v.state, G.WAIT)
        self.assertIn("from the spot", v.text)
        self.assertEqual(g.check(SLOT, at(55), [row("24697.9")], SPOTS).state, G.WAIT)       # changed again
        self.assertEqual(g.check(SLOT, at(65), [row("24697.9")], SPOTS).state, G.GO)         # now agrees with the spot

    def test_after_the_vendor_lag_window_a_stable_candle_is_accepted_without_the_spot_check(self):
        g = G.CandleGuard()
        g.check(SLOT, at(85), [row("24669.8")], SPOTS)
        self.assertEqual(g.check(SLOT, at(95), [row("24669.8")], SPOTS).state, G.GO)

    def test_without_a_spot_near_the_boundary_stability_alone_decides(self):
        far = ((SLOT - dt.timedelta(seconds=300), D("1")),)
        g = G.CandleGuard()
        g.check(SLOT, at(35), [row("24697.9")], far)
        self.assertEqual(g.check(SLOT, at(45), [row("24697.9")], far).state, G.GO)

    def test_a_restart_long_after_the_boundary_is_never_skipped_on_its_first_read(self):
        g = G.CandleGuard()
        self.assertEqual(g.check(SLOT, at(600), [row("24697.9")], ()).state, G.WAIT)         # first look only
        self.assertEqual(g.check(SLOT, at(610), [row("24697.9")], ()).state, G.GO)

    def test_stable_means_unchanged_for_ten_seconds_whatever_the_poll_rate(self):
        """At a 5 s poll two identical reads are only 5 s apart: not yet proof that the vendor stopped updating the candle."""
        g = G.CandleGuard()
        g.check(SLOT, at(40), [row("24697.9")], SPOTS)
        self.assertEqual(g.check(SLOT, at(45), [row("24697.9")], SPOTS).state, G.WAIT)       # 5 s of identical values
        self.assertEqual(g.check(SLOT, at(50), [row("24697.9")], SPOTS).state, G.GO)         # 10 s

    def test_a_change_restarts_the_stability_clock(self):
        g = G.CandleGuard()
        g.check(SLOT, at(40), [row("24690.0")], SPOTS)
        g.check(SLOT, at(45), [row("24690.0")], SPOTS)
        g.check(SLOT, at(50), [row("24697.9")], SPOTS)                                      # it moved: the clock restarts here
        self.assertEqual(g.check(SLOT, at(55), [row("24697.9")], SPOTS).state, G.WAIT)
        self.assertEqual(g.check(SLOT, at(60), [row("24697.9")], SPOTS).state, G.GO)

    def test_a_candle_that_never_stops_changing_is_skipped(self):
        g = G.CandleGuard()
        for i, s in enumerate((35, 50, 65, 80, 95, 110)):
            self.assertEqual(g.check(SLOT, at(s), [row("24690.%d" % i)], SPOTS).state, G.WAIT)
        self.assertEqual(g.check(SLOT, at(125), [row("24690.9")], SPOTS).state, G.SKIP)

    def test_a_new_slot_starts_from_scratch(self):
        g = G.CandleGuard()
        g.check(SLOT, at(35), [row("24697.9")], SPOTS)
        nxt = SLOT + dt.timedelta(minutes=5)
        nrow = row("24700.0", start="2026-11-06T10:30:00+05:30")
        self.assertEqual(g.check(nxt, nxt + dt.timedelta(seconds=45), [nrow], ()).state, G.WAIT)   # first read of this slot


if __name__ == "__main__":
    unittest.main()
