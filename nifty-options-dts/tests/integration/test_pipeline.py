"""End to end, offline: data -> regime -> strategy -> risk -> ticket -> "yes" -> paper entry -> management -> journal."""
import copy
import datetime
import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec

from nifty_core import pipeline as P
from nifty_core.data.fixture_provider import FixtureProvider
from nifty_core.guards import paper_only
from nifty_core.journal import journal as J
from nifty_core.journal import report as REP
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase

BIG = None


def big_cfg():
    """A test configuration with twice the virtual capital, so the fixture candidates fit the per-trade limit."""
    return sc.config()._replace(account_capital_inr=Dec("500000"), risk_per_trade_inr=Dec("5000"),
                                max_daily_loss_inr=Dec("10000"))


class Base(NoNetworkTestCase):
    def setUp(self):
        super(Base, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.kill = os.path.join(self.dir, "trader")

    def run_day(self, name, cfg=None, answers=None, run_id="r1", path=None):
        scenario = sc.scenario(name)
        ctx = P.load_context(cfg or sc.config())
        path = path or os.path.join(self.dir, "%s.jsonl" % name)
        jr = J.Journal(path, J.data_source_for_label(scenario.data_label), run_id)
        summary = P.run_day(FixtureProvider(scenario), scenario.day, ctx, self.kill, jr, answers)
        return summary, J.read_records(path), path

    @staticmethod
    def kinds(records, kind):
        return [r for r in records if r["kind"] == kind]


class EveryFixtureDayTests(Base):
    def test_every_test_day_runs_from_data_to_journal(self):
        for name in sc.NAMES:
            summary, records, path = self.run_day(name)
            self.assertTrue(J.verify(path).ok, name)
            self.assertEqual(records[0]["kind"], "RUN_START", name)
            self.assertEqual(records[-1]["kind"], "RUN_END", name)
            decisions = self.kinds(records, "DECISION")
            self.assertGreater(len(decisions), 40, name)
            self.assertEqual(sum(summary.status_counts.values()), len(decisions), name)
            self.assertEqual((summary.entries, summary.exits, summary.criticals), (0, 0, 0), name)
            self.assertTrue(set(summary.status_counts) <= {"NO_TRADE", "BLOCKED"}, (name, summary.status_counts))
            for r in decisions:
                p = r["payload"]
                self.assertIn(p["status"], ("NO_TRADE", "BLOCKED"))
                self.assertTrue(p["regime"]["label"])
                self.assertTrue(p["inputs_hash"])
                self.assertEqual(r["data_source"], "fixture")
                self.assertIs(r["paper"], True)
                if p["status"] == "NO_TRADE":
                    self.assertTrue(p["strategy"]["reasons"], name)
                else:
                    self.assertEqual(p["risk"]["outcome"], "BLOCKED")
                    self.assertTrue(p["risk"]["reason_codes"])

    def test_no_trade_and_blocked_days_are_both_present(self):
        summary, records, _ = self.run_day("bullish_trend")
        self.assertEqual(summary.status_counts.get("BLOCKED"), 1)
        self.assertGreater(summary.status_counts.get("NO_TRADE"), 40)
        summary, _, _ = self.run_day("sideways")
        self.assertEqual(set(summary.status_counts), {"NO_TRADE"})

    def test_a_replay_is_byte_for_byte_repeatable(self):
        _, _, a = self.run_day("bullish_trend", path=os.path.join(self.dir, "a.jsonl"))
        _, _, b = self.run_day("bullish_trend", path=os.path.join(self.dir, "b.jsonl"))
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())

    def test_fixture_reports_carry_the_not_evidence_line(self):
        _, _, path = self.run_day("bearish_trend")
        rep = REP.build_report(path)
        self.assertEqual(rep["data_source"], "fixture")
        self.assertIn("not evidence of profitability", REP.render_report(rep))

    def test_run_start_states_paper_and_the_fingerprints(self):
        _, records, _ = self.run_day("sideways")
        p = records[0]["payload"]
        self.assertIn("PAPER", p["notice"])
        self.assertIn("SIMULATED", p["notice"])
        for key in ("config_fingerprint", "strategy_thresholds", "regime_thresholds", "position_thresholds"):
            self.assertTrue(p[key], key)


class ApprovalFlowTests(Base):
    def ticket_fp(self, name="bullish_trend"):
        summary, records, _ = self.run_day(name, cfg=big_cfg(), path=os.path.join(self.dir, "probe.jsonl"), run_id="probe")
        self.assertEqual(len(summary.tickets_awaiting), 1)
        return summary.tickets_awaiting[0]

    def test_without_a_yes_nothing_is_entered_and_the_risk_is_released(self):
        summary, records, _ = self.run_day("bullish_trend", cfg=big_cfg())
        self.assertEqual(summary.status_counts["AWAITING_APPROVAL"], 1)
        self.assertEqual(summary.entries, 0)
        self.assertEqual(len(self.kinds(records, "TICKET")), 1)
        self.assertEqual(self.kinds(records, "APPROVAL"), [])
        self.assertEqual(self.kinds(records, "EXECUTION"), [])
        self.assertEqual((summary.final_ledger.trades_today, summary.final_ledger.open_lots,
                          summary.final_ledger.open_risk_reserved_inr), (0, 0, 0))
        ticket = self.kinds(records, "TICKET")[0]["payload"]
        self.assertIn("PAPER ORDER", ticket["text"])
        self.assertEqual(summary.tickets_awaiting, (ticket["fingerprint"],))

    def test_a_recorded_yes_for_the_exact_ticket_enters_a_paper_position_and_it_is_managed_to_an_exit(self):
        fp = self.ticket_fp()
        reply = "yes"
        summary, records, path = self.run_day("bullish_trend", cfg=big_cfg(), answers={fp: reply}, run_id="r2")
        self.assertEqual(summary.entries, 1)
        approval = self.kinds(records, "APPROVAL")[0]["payload"]
        self.assertEqual((approval["reply_verbatim"], approval["accepted"], approval["fingerprint"]), (reply, True, fp))
        ex = self.kinds(records, "EXECUTION")[0]["payload"]
        self.assertEqual((ex["status"], ex["kind"], len(ex["legs"])), ("FILLED", "ENTRY", 2))
        self.assertEqual(ex["approval_id"], self.kinds(records, "TICKET")[0]["payload"]["approval_id"])
        self.assertTrue(J.verify(path).ok)
        self.assertEqual(summary.exits, 1)
        exit_rec = self.kinds(records, "EXIT")[0]["payload"]
        self.assertEqual(exit_rec["exit_reason"], "DATA_SAFETY")
        self.assertEqual(Dec(exit_rec["net_pnl_inr"]), summary.net_pnl_inr)
        self.assertEqual(summary.final_ledger.realized_pnl_inr, summary.net_pnl_inr)
        self.assertEqual((summary.final_ledger.open_lots, summary.final_ledger.open_risk_reserved_inr), (0, 0))
        self.assertEqual(exit_rec["report"]["actual_exit_costs_inr"] is not None, True)
        rep = REP.build_report(path)
        self.assertEqual((rep["completed_trades"], rep["approvals_accepted"]), (1, 1))
        self.assertEqual(rep["net_pnl_inr"], str(summary.net_pnl_inr))

    def test_net_pnl_reconciles_to_gross_minus_costs(self):
        fp = self.ticket_fp()
        _, records, _ = self.run_day("bullish_trend", cfg=big_cfg(), answers={fp: "yes"}, run_id="r2")
        rep = self.kinds(records, "EXIT")[0]["payload"]["report"]
        net = Dec(rep["gross_pnl_inr"]) - Dec(rep["actual_entry_costs_inr"]) - Dec(rep["actual_exit_costs_inr"])
        self.assertEqual(Dec(rep["net_realized_pnl_inr"]), net)
        self.assertIsNone(rep["unrealized"])
        self.assertIn("ESTIMATE", rep["estimated_at_approval"]["label"])

    def test_replies_that_are_not_a_clean_yes_are_recorded_and_decline_the_order(self):
        fp = self.ticket_fp()
        for reply in ("yes?", "ok", "sure", "yes, but only if cheaper", "yes to all", "", "yes 99999", "no"):
            summary, records, _ = self.run_day("bullish_trend", cfg=big_cfg(), answers={fp: reply}, run_id="r3",
                                               path=os.path.join(self.dir, "d%d.jsonl" % abs(hash(reply))))
            approval = self.kinds(records, "APPROVAL")[0]["payload"]
            self.assertEqual((approval["reply_verbatim"], approval["accepted"]), (reply, False), reply)
            self.assertEqual((summary.entries, summary.status_counts.get("DECLINED")), (0, 1), reply)
            self.assertEqual(summary.final_ledger.open_lots, 0)

    def test_an_answer_for_another_fingerprint_does_not_approve_this_ticket(self):
        summary, records, _ = self.run_day("bullish_trend", cfg=big_cfg(), answers={"0123456789abcdef": "yes"})
        self.assertEqual((summary.entries, summary.status_counts.get("AWAITING_APPROVAL")), (0, 1))
        self.assertEqual(self.kinds(records, "APPROVAL"), [])

    def test_the_reply_is_recorded_word_for_word(self):
        fp = self.ticket_fp()
        reply = "Yes,  go ahead   with this one"
        _, records, _ = self.run_day("bullish_trend", cfg=big_cfg(), answers={fp: reply}, run_id="r4")
        self.assertEqual(self.kinds(records, "APPROVAL")[0]["payload"]["reply_verbatim"], reply)

    def test_the_kill_switch_stops_entry_even_with_a_yes(self):
        fp = self.ticket_fp()
        paper_only.trip_kill_switch(self.kill)
        summary, records, _ = self.run_day("bullish_trend", cfg=big_cfg(), answers={fp: "yes"}, run_id="r5")
        self.assertEqual(summary.entries, 0)
        blocked = [r for r in self.kinds(records, "DECISION") if r["payload"]["status"] == "BLOCKED"]
        self.assertIn("KILL_SWITCH_ACTIVE", blocked[0]["payload"]["risk"]["reason_codes"])
        self.assertEqual(self.kinds(records, "APPROVAL"), [])

    def test_a_position_open_at_the_session_end_is_a_critical_exception(self):
        fp = self.ticket_fp("bearish_trend")
        summary, records, _ = self.run_day("bearish_trend", cfg=big_cfg(), answers={fp: "yes"}, run_id="r6")
        self.assertEqual((summary.entries, summary.exits, summary.criticals), (1, 0, 1))
        crit = self.kinds(records, "CRITICAL")[0]["payload"]
        self.assertIn("NOT closed", crit["text"])
        self.assertEqual(self.kinds(records, "EXIT"), [])
        self.assertEqual(summary.final_ledger.open_lots, 1)                    # the ledger still holds it open
        positions = self.kinds(records, "POSITION")
        self.assertTrue(positions)
        self.assertIn("FORCED_FLAT_BY", [p["payload"]["primary_reason"] for p in positions])
        self.assertTrue(any(p["payload"]["forced"] for p in positions))
        rep = REP.build_report(os.path.join(self.dir, "bearish_trend.jsonl"))
        self.assertEqual(rep["critical_exceptions"], 1)
        self.assertEqual(rep["completed_trades"], 0)

    def test_the_day_locks_after_the_last_allowed_trade(self):
        fp = self.ticket_fp()
        cfg = big_cfg()._replace(max_trades_per_day=1)
        summary, records, _ = self.run_day("bullish_trend", cfg=cfg, answers={fp: "yes"}, run_id="r8")
        self.assertEqual((summary.entries, summary.exits), (1, 1))
        locked = self.kinds(records, "DAY_LOCKED")
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["at"], "2026-09-16T14:30:00+05:30")
        self.assertEqual(locked[0]["payload"]["trades_today"], 1)
        later = [r for r in self.kinds(records, "DECISION") if r["at"] > locked[0]["at"]]
        self.assertEqual(later, [])

    def test_no_second_entry_while_a_position_is_open(self):
        fp = self.ticket_fp("bearish_trend")
        _, records, _ = self.run_day("bearish_trend", cfg=big_cfg(), answers={fp: "yes"}, run_id="r7")
        entered = [r for r in self.kinds(records, "DECISION") if r["payload"]["status"] == "ENTERED"]
        self.assertEqual(len(entered), 1)

    def test_the_pipeline_never_approves_by_itself(self):
        import inspect
        sig = inspect.signature(P.run_day)
        self.assertEqual(list(sig.parameters), ["provider", "day", "ctx", "kill_dir", "journal", "answers"])
        self.assertIsNone(sig.parameters["answers"].default)
        summary, _, _ = self.run_day("bullish_trend", cfg=big_cfg(), answers={})
        self.assertEqual(summary.entries, 0)

    def test_a_broken_paper_guard_stops_the_run(self):
        from unittest import mock
        from nifty_core.errors import LiveTradingNotSupported
        with mock.patch.object(paper_only, "PAPER_ONLY", False):
            with self.assertRaises(LiveTradingNotSupported):
                self.run_day("sideways")

    def test_default_limits_are_not_loosened_by_the_pipeline(self):
        summary, _, _ = self.run_day("bearish_trend")
        self.assertEqual(summary.entries, 0)
        self.assertEqual(summary.status_counts.get("BLOCKED"), 1)


if __name__ == "__main__":
    unittest.main()
