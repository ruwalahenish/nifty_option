"""The adaptive profile end to end: the selector decides, a pick must repeat before it trades, and every strategy flows through
the same risk gate, ticket, paper entry, position management and journal.

Offline, on the fixture days: no network, no real token."""
import datetime
import os
import tempfile
import unittest
from decimal import Decimal as Dec
from unittest import mock

from nifty_core import pipeline
from nifty_core.errors import ConfigError
from nifty_core.journal import journal as J
from nifty_core.live_session import LiveSession
from nifty_core.market.ist import parse_ist
from nifty_core.selector import engine as E
from nifty_core.selector import params as SP
from nifty_core.strategy import engine as strategy_engine
from tests import _scenarios as sc
from tests import _selector_helpers as H
from tests._base import NoNetworkTestCase
from tests.integration.test_pipeline import big_cfg
from tests.unit.test_selector import HV, selection, values
from tools import dashboard

DAY = sc.scenario("sideways").day
T1230 = parse_ist("%s 12:30" % DAY)
SEC = datetime.timedelta(seconds=1)


def adaptive_ctx(confirmations=None):
    ctx = pipeline.load_context(big_cfg(), "fixture", "adaptive")
    if confirmations is not None:
        ctx = ctx._replace(selector_params=SP.build_params(values(entry_confirmations=confirmations)))
    return ctx


class Base(NoNetworkTestCase):
    def setUp(self):
        super(Base, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.path = os.path.join(self.dir, "j.jsonl")

    def runner(self, ctx=None, scenario="sideways", auto=True, path=None, restore=False):
        scen = sc.scenario(scenario)
        r = pipeline.DayRunner(sc.provider(scenario), scen.day, ctx or adaptive_ctx(), os.path.join(self.dir, "trader"),
                               J.Journal(path or self.path, "fixture", "r-%d" % id(object())), auto_entry=auto,
                               restore=restore)
        r.start(T1230)
        return r

    def records(self, kind):
        return [r for r in J.read_records(self.path) if r["kind"] == kind]

    def decisions(self):
        return [r["payload"] for r in self.records("DECISION") if not r["payload"].get("follow_up")]


class ProfileTests(NoNetworkTestCase):
    def test_the_adaptive_profile_loads_a_selector_and_no_opening_range_setup(self):
        ctx = pipeline.load_context(big_cfg(), "fixture", "adaptive")
        self.assertIsNotNone(ctx.selector_params)
        self.assertIsNone(ctx.setup_params)
        self.assertEqual(ctx.selector_params.entry_confirmations, 2)

    def test_the_other_profiles_are_unchanged(self):
        for profile in ("intraday", "human", "default", None):
            self.assertIsNone(pipeline.load_context(big_cfg(), "fixture", profile).selector_params, profile)

    def test_the_live_runner_defaults_to_the_adaptive_profile(self):
        from nifty_live import live_run
        self.assertEqual(live_run.build_parser().parse_args([]).profile, "adaptive")

    def test_the_confirmation_count_is_validated(self):
        for bad in (0, 6, True, -1):
            with self.assertRaises(ConfigError, msg=str(bad)):
                SP.build_params(values(entry_confirmations=bad))
        for ok in (1, 5):
            self.assertEqual(SP.build_params(values(entry_confirmations=ok)).entry_confirmations, ok)

    def test_the_confirmation_count_is_part_of_the_fingerprint(self):
        self.assertNotEqual(SP.build_params(values(entry_confirmations=1)).fingerprint,
                            SP.build_params(values(entry_confirmations=3)).fingerprint)


class SlotTimingTests(NoNetworkTestCase):
    def test_the_entry_cut_off_is_a_slot_rule(self):
        late = parse_ist("%s 13:30:07" % DAY)
        self.assertTrue(E._timing(late, "13:30", "credit").passed)              # still the 13:30 slot
        self.assertFalse(E._timing(parse_ist("%s 13:35:07" % DAY), "13:30", "credit").passed)


class ConfirmationTests(Base):
    def sel(self):
        return selection("sideways", "12:30")

    def test_a_pick_must_repeat_before_it_is_taken(self):
        r = self.runner()
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=self.sel()):
            self.assertEqual(r.tick(T1230), "NO_TRADE")
            first = r.last_decision
            self.assertEqual(first["strategy"]["reasons"][0]["code"], "AWAITING_CONFIRMATION")
            self.assertEqual(first["selector"]["awaiting_confirmation"], {"strategy": "IRON_CONDOR", "have": 1, "need": 2})
            self.assertEqual(r.pm.active(), ())
            self.assertEqual(r.tick(T1230 + 10 * SEC), "ENTERED")
        self.assertEqual(len(r.pm.active()), 1)

    def test_a_flicker_never_trades(self):
        r = self.runner()
        condor, nothing = self.sel(), H.decide("bullish_trend", "12:30", H.trending(4, 3))
        self.assertEqual(nothing.outcome, E.NO_TRADE)
        with mock.patch.object(pipeline.selector_engine, "select_at", side_effect=[condor, nothing, condor, nothing]):
            for i in range(4):
                self.assertEqual(r.tick(T1230 + i * SEC), "NO_TRADE")
        self.assertEqual(r.entries, 0)
        self.assertEqual(r.pm.active(), ())

    def test_a_different_pick_restarts_the_count(self):
        r = self.runner()
        condor = self.sel()
        debit = selection("bullish_trend", "12:30")
        with mock.patch.object(pipeline.selector_engine, "select_at", side_effect=[condor, debit, condor]):
            for i in range(3):
                self.assertEqual(r.tick(T1230 + i * SEC), "NO_TRADE")
        self.assertEqual(r.entries, 0)

    def test_one_confirmation_enters_on_the_first_pick(self):
        r = self.runner(adaptive_ctx(confirmations=1))
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=self.sel()):
            self.assertEqual(r.tick(T1230), "ENTERED")

    def test_a_skipped_slot_breaks_the_run(self):
        r = self.runner()
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=self.sel()):
            r.tick(T1230)
            self.assertEqual(r.tick(T1230 + 5 * SEC, skip_entry_reason="candle not final"), "SLOT_SKIPPED")
            self.assertEqual(r.tick(T1230 + 10 * SEC), "NO_TRADE")             # back to 1 of 2


class EntryTests(Base):
    def enter_condor(self, auto=True):
        r = self.runner(auto=auto)
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
            status = r.tick(T1230 + 10 * SEC)
        return r, status

    def test_the_condor_is_entered_through_the_whole_stack_and_journaled(self):
        r, status = self.enter_condor()
        self.assertEqual(status, "ENTERED")
        entered = [d for d in self.decisions() if d["status"] == "ENTERED"][0]
        self.assertEqual(entered["strategy"]["candidate"]["strategy_type"], "IRON_CONDOR")
        self.assertEqual(len(entered["strategy"]["candidate"]["legs"]), 4)
        self.assertIn("exit_plan", entered["strategy"]["candidate"])
        self.assertEqual(entered["risk"]["outcome"], "APPROVED")
        self.assertLessEqual(Dec(entered["risk"]["total_defined_risk_inr"]), Dec(entered["risk"]["per_trade_limit_inr"]))
        self.assertEqual(len(entered["selector"]["evaluations"]), 4)           # how all four strategies were judged
        execution = self.records("EXECUTION")[0]["payload"]
        self.assertEqual((execution["status"], len(execution["legs"])), ("FILLED", 4))
        self.assertIn("Iron Condor", self.records("TICKET")[0]["payload"]["text"])
        self.assertEqual(self.records("APPROVAL")[0]["payload"]["source"], "auto_paper_entry")

    def test_the_decision_record_says_which_strategy_was_judged_and_why(self):
        self.enter_condor()
        d = self.decisions()[0]
        self.assertEqual(d["regime"]["label"], "RANGE_BOUND")
        self.assertIn("range bound", d["selector"]["market"])
        names = [e["strategy"] for e in d["selector"]["evaluations"]]
        self.assertEqual(names, ["Directional Debit Spread", "Directional Credit Spread", "Iron Condor", "Iron Butterfly"])
        condor = d["selector"]["evaluations"][2]
        self.assertTrue(condor["eligible"])
        self.assertTrue(all("detail" in c for c in condor["conditions"]))

    def test_manual_mode_holds_the_condor_for_a_yes_and_only_that_exact_yes_enters(self):
        scen = sc.scenario("sideways")
        r = pipeline.DayRunner(sc.provider("sideways"), scen.day, adaptive_ctx(), os.path.join(self.dir, "trader"),
                               J.Journal(self.path, "fixture", "m"), hold_unanswered=True)
        r.start(T1230)
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
            self.assertEqual(r.tick(T1230 + 10 * SEC), "AWAITING_APPROVAL")
        fp = next(iter(r.pending))
        self.assertEqual(r.pm.active(), ())                                     # nothing entered without the yes
        self.assertEqual(r.answer(fp, "yes", T1230 + 20 * SEC), "ENTERED")
        self.assertEqual(len(r.pm.active()), 1)

    def test_the_risk_gate_still_blocks_an_over_limit_condor(self):
        cfg = big_cfg()._replace(risk_per_trade_inr=Dec("100"), max_daily_loss_inr=Dec("10000"))
        ctx = pipeline.load_context(cfg, "fixture", "adaptive")
        r = pipeline.DayRunner(sc.provider("sideways"), DAY, ctx, os.path.join(self.dir, "trader"),
                               J.Journal(self.path, "fixture", "b"), auto_entry=True)
        r.start(T1230)
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
            self.assertEqual(r.tick(T1230 + 10 * SEC), "BLOCKED")
        self.assertEqual(r.pm.active(), ())
        self.assertIn("PER_TRADE_RISK_EXCEEDED", self.decisions()[-1]["risk"]["reason_codes"])

    def test_the_kill_switch_stops_a_selector_entry_at_the_selector_and_again_at_the_risk_gate(self):
        os.makedirs(os.path.join(self.dir, "trader"), exist_ok=True)
        open(os.path.join(self.dir, "trader", "KILL"), "w").close()
        r = self.runner(adaptive_ctx(confirmations=1))
        self.assertEqual(r.tick(T1230), "NO_TRADE")                             # the selector itself says no
        self.assertIn("KILL_SWITCH", [x["code"] for x in r.last_decision["strategy"]["reasons"]])
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            self.assertEqual(r.tick(T1230 + 5 * SEC), "BLOCKED")                # and, if it did not, so does the risk gate
        self.assertIn("KILL_SWITCH_ACTIVE", self.decisions()[-1]["risk"]["reason_codes"])
        self.assertEqual((r.entries, r.pm.active()), (0, ()))


class ManagementTests(Base):
    def test_the_open_condor_is_managed_with_the_selector_exit_function_and_its_plan(self):
        r = EntryTests.enter_condor(self)[0]
        pos = r.pm.active()[0]
        epos = r.ex.position(pos.position_id)
        self.assertEqual(epos.candidate.strategy_type, "IRON_CONDOR")
        self.assertEqual(pos.entry_regime, "NEUTRAL")
        # at the next chain (14:30) the position is evaluated: it either holds or closes, and never raises
        results = r.pm.manage_all(parse_ist("%s 14:30" % DAY))
        self.assertEqual(len(results), 1)
        self.assertIn(results[0].decision.action, ("HOLD", "CLOSE"))

    def test_a_restart_restores_a_multi_leg_trade_into_the_ledger(self):
        EntryTests.enter_condor(self)
        r2 = self.runner(restore=True)
        self.assertEqual(len(r2._carried), 1)
        position = r2.mgr.ledger.positions[0]
        self.assertEqual(len(position.long_symbol.split("+")), 2)
        self.assertEqual(len(position.short_symbol.split("+")), 2)
        self.assertEqual(r2.mgr.snapshot().trades_today, 1)                     # the day's trade count carries on


class LiveViewTests(Base):
    def test_the_live_snapshot_and_dashboard_show_how_each_strategy_was_judged(self):
        scen = sc.scenario("sideways")
        s = LiveSession(adaptive_ctx(), os.path.join(self.dir, "trader"), J.Journal(self.path, "fixture", "l"), scen.day)
        s.step(T1230 + 7 * SEC, scen)
        view = s.snapshot(T1230 + 8 * SEC)
        sel = view["last_decision"]["selector"]
        self.assertEqual(len(sel["evaluations"]), 4)
        self.assertEqual(sel["selected_title"], "Iron Condor")
        html = dashboard._selector_card(sel)
        for text in ("Strategy selector", "Iron Condor", "SELECTED", "Directional Debit Spread", "not eligible",
                     "Pick 1 of 2 consecutive slots"):
            self.assertIn(text, html)

    def test_an_exit_plan_position_shows_gross_levels_net_of_costs_on_the_gauge(self):
        EntryTests.enter_condor(self)
        runner = pipeline.DayRunner(sc.provider("sideways"), DAY, adaptive_ctx(), os.path.join(self.dir, "t2"),
                                    J.Journal(os.path.join(self.dir, "x.jsonl"), "fixture", "x"), auto_entry=True)
        runner.start(T1230)
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            runner.tick(T1230)
            runner.tick(T1230 + 10 * SEC)
        s = LiveSession.__new__(LiveSession)
        epos = runner.ex.position(runner.pm.active()[0].position_id)
        stop, target = LiveSession._exit_levels(epos, epos.approval.defined_risk_inr, None,
                                                runner.ctx.position_params)
        plan = epos.candidate.exit_plan
        self.assertLess(stop, -Dec(plan["stop_gross_inr"]) + 1)                 # costs make the net stop a little deeper
        self.assertLess(target, Dec(plan["target_gross_inr"]))                  # and the net target a little lower
        self.assertGreater(target, 0)


class DebitPathUnchangedTests(Base):
    def test_a_debit_pick_still_goes_through_the_original_candidate_and_ticket(self):
        r = self.runner(scenario="bullish_trend")
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("bullish_trend", "12:30")):
            t = parse_ist("%s 12:30" % sc.scenario("bullish_trend").day)
            r.tick(t)
            self.assertEqual(r.tick(t + 10 * SEC), "ENTERED")
        entered = [d for d in self.decisions() if d["status"] == "ENTERED"][0]
        self.assertEqual(entered["strategy"]["candidate"]["strategy_type"], "BULL_CALL_DEBIT")
        self.assertIn("long_contract", entered["strategy"]["candidate"])
        self.assertEqual([l["leg"] for l in self.records("EXECUTION")[0]["payload"]["legs"]], ["long", "short"])


if __name__ == "__main__":
    unittest.main()
