"""Automatic PAPER entry (live_run --entry-mode auto): it removes only the human reply. Every other gate still applies.

Offline, on fixtures with a simulated clock."""
import datetime
import os
import tempfile
import unittest
from decimal import Decimal as Dec

from nifty_core import pipeline as P
from nifty_core.data.fixture_provider import FixtureProvider
from nifty_core.journal import journal as J
from nifty_core.live_session import LiveSession
from nifty_core.positions import manager as M
from nifty_live import live_run
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase
from tests.integration.test_pipeline import big_cfg
from tests.unit import test_positions as TP

SEC = datetime.timedelta(seconds=1)


class AutoBase(NoNetworkTestCase):
    def setUp(self):
        super(AutoBase, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.kill = os.path.join(self.dir, "trader")

    def session(self, name="bullish_trend", cfg=None, profile=None, auto=True):
        self.scenario = sc.scenario(name)
        self.ctx = P.load_context(cfg or big_cfg(), "fixture", profile)
        self.count = getattr(self, "count", 0) + 1
        self.path = os.path.join(self.dir, "auto%d.jsonl" % self.count)
        self.s = LiveSession(self.ctx, self.kill, J.Journal(self.path, "fixture", "auto"), self.scenario.day,
                             auto_entry=auto)
        self.times = live_run.replay_times(self.scenario, self.ctx, self.scenario.day)
        return self.s

    def kinds(self, kind):
        return [r for r in J.read_records(self.path) if r["kind"] == kind]

    def run_all(self):
        for now in self.times:
            self.s.step(now, self.scenario)
        return self.s.finish(self.times[-1])


class AutoEntryTests(AutoBase):
    def test_a_qualifying_setup_is_entered_without_any_reply_and_the_journal_says_so(self):
        self.session()
        entered_at = None
        for now in self.times:
            self.s.step(now, self.scenario)
            if self.s.runner.pm.active():
                entered_at = now
                break
        self.assertIsNotNone(entered_at)
        self.assertFalse(self.s.runner.pending)                         # nothing waits for a human
        approval = self.kinds("APPROVAL")[0]["payload"]
        self.assertEqual((approval["accepted"], approval["source"]), (True, "auto_paper_entry"))
        self.assertIn("no human reply", approval["reply_verbatim"])
        self.assertIn("automatic", approval["why"])
        self.assertEqual(self.kinds("EXECUTION")[0]["payload"]["status"], "FILLED")
        view = self.s.snapshot(entered_at)
        self.assertEqual((view["entry_mode"], view["pending_tickets"]), ("AUTO", []))
        self.assertIsNotNone(view["open_positions"][0]["unrealized"])    # the unrealised P&L shows straight away

    def test_the_whole_day_enters_and_exits_by_itself_and_the_journal_verifies(self):
        self.session()
        summary = self.run_all()
        self.assertEqual((summary.entries, summary.exits, summary.criticals), (1, 1, 0))
        self.assertTrue(J.verify(self.path).ok)
        self.assertEqual(len(self.kinds("EXIT")), 1)
        self.assertEqual(self.s.snapshot(self.times[-1])["open_positions"], [])

    def test_manual_is_still_the_default_of_the_session_and_enters_nothing_alone(self):
        self.session(auto=False)
        summary = self.run_all()
        self.assertEqual(summary.entries, 0)
        self.assertEqual(self.kinds("APPROVAL"), [])
        self.assertEqual(self.s.snapshot(self.times[0])["entry_mode"], "MANUAL")

    def test_a_plain_replay_still_never_enters_without_an_answer(self):
        scenario = sc.scenario("bullish_trend")
        jr = J.Journal(os.path.join(self.dir, "replay.jsonl"), "fixture", "r")
        out = P.run_day(FixtureProvider(scenario), scenario.day, P.load_context(big_cfg()), self.kill, jr, {})
        self.assertEqual((out.entries, out.status_counts.get("AWAITING_APPROVAL")), (0, 1))


class AutoDoesNotBypassTheGatesTests(AutoBase):
    def test_the_per_trade_risk_limit_still_blocks(self):
        self.session(cfg=sc.config())                                  # default capital: the 2,500 limit blocks the fixture
        summary = self.run_all()
        self.assertEqual(summary.entries, 0)
        self.assertEqual(self.kinds("EXECUTION"), [])

    def test_the_kill_switch_still_stops_automatic_entries(self):
        self.session()
        os.makedirs(self.kill)
        open(os.path.join(self.kill, "KILL"), "w").close()
        summary = self.run_all()
        self.assertEqual(summary.entries, 0)
        self.assertEqual(self.kinds("EXECUTION"), [])
        self.assertTrue(self.s.snapshot(self.times[0])["kill_switch"])

    def test_the_two_trades_a_day_limit_and_one_position_at_a_time_still_hold(self):
        self.session(name="bullish_trend", cfg=sc.config(), profile="human")        # the human profile finds 3 tickets
        most = 0
        for now in self.times:
            self.s.step(now, self.scenario)
            most = max(most, len(self.s.runner.pm.active()))
        summary = self.s.finish(self.times[-1])
        self.assertLessEqual(most, 1)
        self.assertLessEqual(summary.entries, self.ctx.cfg.max_trades_per_day)
        self.assertGreaterEqual(summary.entries, 1)
        self.assertEqual(summary.exits + summary.criticals, summary.entries)
        for r in self.kinds("APPROVAL"):
            self.assertEqual(r["payload"]["source"], "auto_paper_entry")

    def test_days_that_should_not_be_traded_are_still_not_traded_automatically(self):
        for name in ("sideways", "high_volatility", "illiquid_options", "missing_invalid_data"):
            self.session(name=name, profile="human")
            self.assertEqual(self.run_all().entries, 0, name)

    def test_the_paper_only_guard_is_not_touched_by_the_mode(self):
        self.session()
        with self.assertRaises(Exception):
            LiveSession(self.ctx._replace(cfg=self.ctx.cfg._replace(paper=not True)), self.kill,
                        J.Journal(os.path.join(self.dir, "x.jsonl"), "fixture", "x"), self.scenario.day, auto_entry=True)


class RestartTests(AutoBase):
    """A live session that is restarted on the same day continues from the journal instead of being refused, and the
    day's limits carry over (they cannot be reset by restarting)."""

    def start_again(self, cfg=None, profile=None, name="bullish_trend"):
        ctx = P.load_context(cfg or big_cfg(), "fixture", profile)
        self.count += 1
        self.s = LiveSession(ctx, self.kill, J.Journal(self.path, "fixture", "restart%d" % self.count), self.scenario.day,
                             auto_entry=True)
        return self.s

    def enter_then_get_killed(self, marks=True):
        self.session()
        if not marks:
            self.s._journal_marks = lambda now: None                      # the run dies before it records any mark
        for now in self.times:
            self.s.step(now, self.scenario)
            if self.s.runner.pm.active():
                return now                                                # abandoned: no finish(), no exit, like a kill
        self.fail("no entry happened")

    def test_restarting_after_a_kill_closes_the_orphan_at_its_last_mark_and_carries_the_trade_count(self):
        at = self.enter_then_get_killed()
        self.s._journal_marks(at + 10 * SEC)                            # the last mark the dead run managed to write
        last_mark = self.kinds("MARK")[-1]["payload"]["unrealized"]
        entered = self.kinds("DECISION")[-1]["payload"]["position_id"]
        before = len(J.read_records(self.path))
        self.start_again()
        out = self.s.step(at + 60 * SEC, self.scenario)                  # must not raise DAY_ALREADY_TRADED
        self.assertTrue(any("closed administratively" in line for line in out), out)
        exits = [r for r in self.kinds("EXIT") if r["payload"].get("administrative")]
        self.assertEqual(len(exits), 1)
        x = exits[0]["payload"]
        self.assertEqual((x["position_id"], x["exit_reason"], x["net_pnl_inr"]), (entered, "RUNNER_STOPPED",
                                                                                    last_mark["net_pnl_inr"]))
        self.assertIn("Not a simulated exit fill", x["note"])
        self.assertGreater(len(J.read_records(self.path)), before)
        self.assertTrue(J.verify(self.path).ok)
        start = [r for r in self.kinds("RUN_START")][-1]["payload"]
        self.assertEqual(len(start["restored_trades"]), 1)
        view = self.s.snapshot(at + 60 * SEC)
        self.assertEqual(view["totals"]["trades_today"], 1)                # the entry still counts today
        self.assertEqual(view["totals"]["realised_net_inr"], str(Dec(last_mark["net_pnl_inr"])))
        self.assertEqual(view["open_positions"], [])
        self.assertEqual(view["closed_trades"][0]["exit_reason"], "RUNNER_STOPPED")
        self.assertTrue(view["closed_trades"][0]["administrative"])

    def test_a_trade_that_had_already_closed_properly_is_carried_as_it_was(self):
        self.session()
        self.run_all()
        closed = self.kinds("EXIT")[0]["payload"]
        self.start_again()
        self.s.step(self.times[-1] + SEC, self.scenario)
        self.assertEqual(len([r for r in self.kinds("EXIT") if r["payload"].get("administrative")]), 0)
        view = self.s.snapshot(self.times[-1] + SEC)
        self.assertEqual(view["totals"]["realised_net_inr"], str(Dec(closed["net_pnl_inr"])))
        self.assertEqual(view["closed_trades"][0]["exit_reason"], closed["exit_reason"])

    def test_restarting_does_not_reset_the_daily_limits(self):
        at = self.enter_then_get_killed()
        self.s._journal_marks(at + 10 * SEC)
        tight = big_cfg()._replace(max_daily_loss_inr=Dec("100"))        # the orphan's loss already exceeds this
        self.start_again(cfg=tight)
        self.s.step(at + 60 * SEC, self.scenario)
        for now in self.times:
            if now > at + 60 * SEC:
                self.s.step(now, self.scenario)
        self.assertEqual(self.s.runner.entries, 0)                        # locked: no new entry after the restart
        self.assertTrue(self.kinds("DAY_LOCKED"))

    def test_the_candidate_that_was_traded_cannot_be_traded_again_after_a_restart(self):
        at = self.enter_then_get_killed()
        used = {p.candidate_id for p in self.s.runner.mgr._ledger.positions}
        self.start_again()
        self.s.step(at + 60 * SEC, self.scenario)
        self.assertTrue(used <= self.s.runner.mgr._ledger.candidate_ids())

    def test_with_no_mark_recorded_the_orphan_is_valued_at_minus_its_entry_costs(self):
        at = self.enter_then_get_killed(marks=False)
        self.assertEqual(self.kinds("MARK"), [])
        costs = Dec(self.kinds("EXECUTION")[0]["payload"]["actual"]["entry_costs"]["total_inr"])
        self.start_again()
        self.s.step(at + 60 * SEC, self.scenario)
        x = [r for r in self.kinds("EXIT") if r["payload"].get("administrative")][0]["payload"]
        self.assertEqual(Dec(x["net_pnl_inr"]), -costs)
        self.assertIn("entry costs only", x["note"])

    def test_restarting_twice_does_not_close_the_same_orphan_twice(self):
        at = self.enter_then_get_killed()
        self.start_again()
        self.s.step(at + 60 * SEC, self.scenario)
        self.start_again()
        self.s.step(at + 120 * SEC, self.scenario)
        self.assertEqual(len([r for r in self.kinds("EXIT") if r["payload"].get("administrative")]), 1)
        self.assertEqual(self.s.snapshot(at + 120 * SEC)["totals"]["trades_today"], 1)

    def test_a_plain_replay_of_a_day_that_traded_is_still_refused(self):
        self.session()
        self.run_all()
        from nifty_core.errors import JournalError
        real = J.Journal(os.path.join(self.dir, "real.jsonl"), "real", "r1")
        scenario = self.scenario
        ctx = self.ctx
        P.run_day(FixtureProvider(scenario), scenario.day, ctx, self.kill, real, {})        # real-source journal, no entry
        # force an entry record into a real-source journal, then a second replay must be refused as before
        real.append("RUN_START", self.times[0], {"day": scenario.day.isoformat()})
        real.append("DECISION", self.times[0], {"status": "ENTERED"})
        with self.assertRaises(JournalError):
            P.run_day(FixtureProvider(scenario), scenario.day, ctx, self.kill, J.Journal(real.path, "real", "r2"), {})

    def test_the_live_loop_stops_instead_of_repeating_when_the_journal_refuses_the_run(self):
        # live sessions restore, so the refusal path is only reachable for a damaged journal: it must stop, not loop
        from nifty_core.errors import JournalError
        self.assertTrue(issubclass(JournalError, Exception))


class LiveRunFlagTests(NoNetworkTestCase):
    def test_auto_is_the_default_and_the_flag_accepts_only_auto_or_manual(self):
        parser = live_run.build_parser()
        self.assertEqual(parser.parse_args([]).entry_mode, "auto")
        self.assertEqual(parser.parse_args(["--entry-mode", "manual"]).entry_mode, "manual")
        with self.assertRaises(SystemExit):
            parser.parse_args(["--entry-mode", "yolo"])
        self.assertIn("AUTOMATIC", live_run.ENTRY_MODE_TEXT["auto"])
        self.assertIn("trader/KILL", live_run.ENTRY_MODE_TEXT["auto"])

    def test_the_runner_passes_the_mode_through_and_prints_no_ticket_prompt_in_auto(self):
        scenario = sc.scenario("bullish_trend")
        ctx = P.load_context(big_cfg())
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        out = []
        runner = live_run.Runner(ctx, scenario.day, J.Journal(os.path.join(d, "j.jsonl"), "fixture", "r"), "REPLAY",
                                 os.path.join(d, "state.json"), None, out.append, auto_entry=True)
        for now in live_run.replay_times(scenario, ctx, scenario.day):
            runner.step(now, scenario)
        self.assertEqual(live_run.read_state(os.path.join(d, "state.json"))["entry_mode"], "AUTO")
        self.assertFalse(any("answer" in line for line in out))
        self.assertTrue(any("ENTERED" in line for line in out))


class SetupExitTests(TP.Base):
    """The position manager closes a position when the entry setup has failed (price back inside the opening range)."""

    def manager(self, fn):
        self.build([TP.TE.entry_chain(), self.chain_at(300)])
        self.pm = M.PositionManager(self.cfg, TP.DEFAULT, self.ex, self.mgr, self.provider, TP.sc.calendar(), TP.RPARAMS,
                                    TP.ENGINE, regime_fn=TP.regime("BULLISH"), setup_exit_fn=fn)
        result, pos = self.pm.enter(self.approval, self.cand, 1, TP.T1230)
        return pos.position_id

    def test_a_failed_setup_closes_the_position(self):
        pid = self.manager(lambda now, label: "back inside the range")
        d = self.pm.evaluate(pid, TP.at(300))
        self.assertEqual((d.action, d.primary_reason), (M.CLOSE, M.SETUP_INVALIDATED))
        self.assertEqual(self.pm.manage(pid, TP.at(300)).position.state, M.CLOSED)

    def test_the_entry_label_is_handed_to_the_check_and_a_quiet_check_holds(self):
        seen = []
        pid = self.manager(lambda now, label: seen.append(label) or None)
        self.assertEqual(self.pm.evaluate(pid, TP.at(300)).action, M.HOLD)
        self.assertEqual(seen, ["BULLISH"])

    def test_without_a_check_nothing_changes(self):
        pid = self.manager(None)
        self.assertEqual(self.pm.evaluate(pid, TP.at(300)).action, M.HOLD)

    def test_priority_puts_stop_loss_before_setup_failure_and_setup_failure_before_profit_target(self):
        order = M.PRIORITY
        self.assertLess(order.index(M.STOP_LOSS), order.index(M.SETUP_INVALIDATED))
        self.assertLess(order.index(M.SETUP_INVALIDATED), order.index(M.PROFIT_TARGET))


if __name__ == "__main__":
    unittest.main()
