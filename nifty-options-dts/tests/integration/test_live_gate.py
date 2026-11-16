"""A slot is only decided on a final candle: the live session holds it (WAIT), or skips new entries (SKIP).

Offline, on a fixture with a simulated clock: no network, no real token."""
import datetime

from nifty_core.market.ist import ist_datetime
from nifty_live.candle_guard import GO, SKIP, WAIT, Verdict
from tests.integration.test_live_session import SEC, LiveBase


class GateTests(LiveBase):
    def slot_time(self, hh, mm):
        return ist_datetime(self.scenario.day, datetime.time(hh, mm))

    def test_a_waiting_slot_decides_nothing_and_says_so_once(self):
        now = self.slot_time(11, 0) + 7 * SEC
        out = self.session.step(now, self.scenario, lambda slot: Verdict(WAIT, "the 10:55 candle changed"))
        self.assertEqual(len(out), 1)
        self.assertIn("waiting for its final candle", out[0])
        self.assertEqual(self.kinds("DECISION"), [])
        self.assertIsNone(self.session.last_slot)
        again = self.session.step(now + 10 * SEC, self.scenario, lambda slot: Verdict(WAIT, "the 10:55 candle changed"))
        self.assertEqual(again, [])                                           # not repeated every poll

    def test_the_slot_is_decided_as_soon_as_the_candle_is_final(self):
        now = self.slot_time(11, 0) + 7 * SEC
        self.session.step(now, self.scenario, lambda slot: Verdict(WAIT, "settling"))
        self.session.step(now + 40 * SEC, self.scenario, lambda slot: Verdict(GO, "final"))
        self.assertEqual(self.session.last_slot, self.slot_time(11, 0))
        self.assertEqual(len(self.kinds("DECISION")), 1)
        self.session.step(now + 50 * SEC, self.scenario, lambda slot: Verdict(GO, "final"))
        self.assertEqual(len(self.kinds("DECISION")), 1)                      # once per slot, as before

    def test_a_skipped_slot_takes_no_new_entry_and_is_journaled_with_its_reason(self):
        now = self.slot_time(11, 0) + 7 * SEC
        out = self.session.step(now, self.scenario, lambda slot: Verdict(SKIP, "still changing after 125 s"))
        self.assertTrue(any("SKIPPED" in line for line in out))
        d = self.kinds("DECISION")
        self.assertEqual([r["payload"]["status"] for r in d], ["SLOT_SKIPPED"])
        self.assertIn("still changing", d[0]["payload"]["reason"])
        self.assertEqual((self.kinds("TICKET"), self.kinds("EXECUTION")), ([], []))
        self.assertEqual(self.session.runner.status_counts["SLOT_SKIPPED"], 1)
        self.session.step(self.slot_time(11, 5) + 7 * SEC, self.scenario)     # the next slot decides normally
        self.assertNotEqual(self.statuses()[-1], "SLOT_SKIPPED")

    def test_without_a_gate_nothing_changes(self):
        self.session.step(self.slot_time(11, 0) + 7 * SEC, self.scenario)
        self.assertEqual(len(self.kinds("DECISION")), 1)


class SlotCutOffTests(LiveBase):
    def test_the_last_entry_slot_still_decides_when_the_poll_runs_seconds_late(self):
        end = self.ctx.cfg.entry_window_end_ist                               # "14:30"
        hh, mm = int(end[:2]), int(end[3:])
        day = self.scenario.day
        late = ist_datetime(day, datetime.time(hh, mm)) + 7 * SEC
        self.session.step(late, self.scenario)
        self.assertEqual(len(self.kinds("DECISION")), 1)                      # was lost: 14:30:07 counted as after 14:30
        nxt = ist_datetime(day, datetime.time(hh, mm)) + datetime.timedelta(minutes=5, seconds=7)
        self.session.step(nxt, self.scenario)
        self.assertEqual(len(self.kinds("DECISION")), 1)                      # the slot after the window is still refused
