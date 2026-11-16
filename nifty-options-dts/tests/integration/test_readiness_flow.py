"""Trade readiness through the whole system: the journal, the live session, the dashboard page and the JSON API.

Offline, on the fixture days with a simulated clock: no network, no real token."""
import datetime
import io
import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec
from unittest import mock

from nifty_core import pipeline
from nifty_core.journal import journal as J
from nifty_core.live_session import LiveSession
from nifty_core.market.ist import parse_ist
from nifty_core.selector import engine as E
from nifty_core.selector import readiness as RD
from tests import _scenarios as sc
from tests import _selector_helpers as H
from tests._base import NoNetworkTestCase
from tests.integration.test_adaptive_pipeline import adaptive_ctx
from tests.integration.test_pipeline import big_cfg
from tests.unit.test_selector import selection
from tools import dashboard
from nifty_live import live_run

DAY = sc.scenario("sideways").day
T1230 = parse_ist("%s 12:30" % DAY)
SEC = datetime.timedelta(seconds=1)


class Base(NoNetworkTestCase):
    def setUp(self):
        super(Base, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.path = os.path.join(self.dir, "j.jsonl")

    def runner(self, ctx=None, auto=True, scenario="sideways"):
        r = pipeline.DayRunner(sc.provider(scenario), sc.scenario(scenario).day, ctx or adaptive_ctx(),
                               os.path.join(self.dir, "trader"), J.Journal(self.path, "fixture", "r"), auto_entry=auto,
                               hold_unanswered=not auto)
        r.start(T1230)
        return r

    def decisions(self):
        return [r["payload"] for r in J.read_records(self.path) if r["kind"] == "DECISION" and not r["payload"].get("follow_up")]


class JournalTests(Base):
    def test_every_adaptive_decision_is_journaled_with_its_readiness(self):
        r = self.runner()
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
            r.tick(T1230 + 10 * SEC)
        first, second = self.decisions()
        self.assertEqual(first["readiness"]["status"], RD.WAITING_CONFIRMATION)
        self.assertEqual(second["readiness"]["status"], RD.EXECUTED)                # the journal carries the FINAL status
        self.assertEqual(second["readiness"]["final_status"], "ENTERED")
        self.assertTrue(second["readiness"]["entry"]["available"])
        self.assertEqual(len(second["readiness"]["strategies"]), 4)

    def test_the_journaled_readiness_matches_what_the_session_shows(self):
        r = self.runner()
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
        self.assertEqual(r.last_decision["readiness"], self.decisions()[-1]["readiness"])

    def test_a_risk_block_is_execution_blocked_in_the_journal_with_the_reason(self):
        cfg = big_cfg()._replace(risk_per_trade_inr=Dec("100"), max_daily_loss_inr=Dec("10000"))
        r = self.runner(ctx=pipeline.load_context(cfg, "fixture", "adaptive"))
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
            self.assertEqual(r.tick(T1230 + 10 * SEC), "BLOCKED")
        ready = self.decisions()[-1]["readiness"]
        self.assertEqual(ready["status"], RD.BLOCKED)
        self.assertIn("risk validation failed", ready["next_action"])
        self.assertEqual(next(x for x in ready["checklist"] if x["name"] == "risk gate and paper execution")["status"], RD.FAILED)

    def test_in_manual_mode_it_is_ready_to_execute_only_after_the_risk_gate_then_executed_after_the_yes(self):
        r = self.runner(auto=False)
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
            self.assertEqual(r.last_decision["readiness"]["status"], RD.WAITING_CONFIRMATION)
            self.assertEqual(r.tick(T1230 + 10 * SEC), "AWAITING_APPROVAL")
        self.assertEqual(r.last_decision["readiness"]["status"], RD.READY_EXECUTE)
        fp = next(iter(r.pending))
        self.assertEqual(r.answer(fp, "yes", T1230 + 20 * SEC), "ENTERED")
        self.assertEqual(r.last_decision["readiness"]["status"], RD.EXECUTED)      # kept in step with the ticket

    def test_an_expired_ticket_is_blocked_not_executed(self):
        r = self.runner(auto=False)
        with mock.patch.object(pipeline.selector_engine, "select_at", return_value=selection("sideways", "12:30")):
            r.tick(T1230)
            r.tick(T1230 + 10 * SEC)
        r.expire_pending(T1230 + 200 * SEC)
        self.assertEqual(r.last_decision["readiness"]["status"], RD.BLOCKED)

    def test_a_real_expiry_problem_is_journaled_as_a_technical_block_with_the_four_strategies(self):
        """The situation behind 'no listed expiry is at least 2 days away': the market is readable, trading is not possible."""
        ctx = adaptive_ctx()
        ctx = ctx._replace(strategy_params=ctx.strategy_params._replace(min_days_to_expiry=30))
        r = self.runner(ctx=ctx)
        self.assertEqual(r.tick(T1230), "NO_TRADE")
        d = self.decisions()[-1]
        self.assertEqual(d["strategy"]["reasons"][0]["code"], "NO_ELIGIBLE_EXPIRY")
        ready = d["readiness"]
        self.assertEqual((ready["status"], ready["technical"][0]["kind"]), (RD.BLOCKED, "EXPIRY"))
        self.assertEqual(ready["entry"]["label"], "ENTRY NOT AVAILABLE")
        self.assertEqual(len(ready["strategies"]), 4)
        self.assertEqual(ready["evaluated_strategy"], "Iron Condor")                 # the one whose market conditions all pass

    def test_the_other_profiles_are_untouched(self):
        scen = sc.scenario("bullish_trend")
        r = pipeline.DayRunner(sc.provider("bullish_trend"), scen.day, pipeline.load_context(big_cfg(), "fixture", "intraday"),
                               os.path.join(self.dir, "t"), J.Journal(self.path, "fixture", "o"), auto_entry=True)
        r.start(T1230)
        r.tick(parse_ist("%s 12:30" % scen.day))
        self.assertNotIn("readiness", r.last_decision)


class SessionTests(Base):
    def session(self, ctx=None, auto=False):
        return LiveSession(ctx or adaptive_ctx(), os.path.join(self.dir, "trader"), J.Journal(self.path, "fixture", "l"), DAY,
                           auto_entry=auto)

    def test_the_snapshot_carries_the_readiness_the_history_and_the_legend(self):
        s = self.session()
        scen = sc.scenario("sideways")
        s.step(T1230 + 7 * SEC, scen)
        view = s.snapshot(T1230 + 8 * SEC)
        d = view["last_decision"]
        self.assertEqual(d["readiness"]["status"], RD.WAITING_CONFIRMATION)
        self.assertEqual(len(view["decision_history"]), 1)
        self.assertEqual(view["decision_history"][0]["strategy"], "Iron Condor")
        self.assertIn("PASSED", view["readiness_legend"])
        json.dumps(view, default=str)

    def test_the_history_grows_one_row_per_decision_and_marks_a_traded_row(self):
        s = self.session(adaptive_ctx(confirmations=1), auto=True)
        scen = sc.scenario("sideways")
        s.step(T1230 + 7 * SEC, scen)
        view = s.snapshot(T1230 + 8 * SEC)
        self.assertEqual([h["traded"] for h in view["decision_history"]], [True])
        self.assertEqual(view["decision_history"][0]["status"], RD.EXECUTED)

    def test_a_restart_in_the_middle_of_the_day_rebuilds_the_history_from_the_journal(self):
        s = self.session()
        s.step(T1230 + 7 * SEC, sc.scenario("sideways"))
        before = s.snapshot(T1230 + 8 * SEC)["decision_history"]
        again = LiveSession(adaptive_ctx(), os.path.join(self.dir, "trader"), J.Journal(self.path, "fixture", "l2"), DAY)
        again.step(T1230 + 20 * SEC, sc.scenario("sideways"))     # a restarted session decides the current slot again
        history = again.snapshot(T1230 + 21 * SEC)["decision_history"]
        self.assertEqual(len(history), 2)                         # the earlier row came back from the journal, plus the new one
        self.assertEqual(history[0], before[0])

    def test_a_skipped_slot_adds_no_history_row(self):
        from nifty_live.candle_guard import SKIP, Verdict
        s = self.session()
        s.step(T1230 + 7 * SEC, sc.scenario("sideways"), lambda slot: Verdict(SKIP, "candle never settled"))
        self.assertEqual(s.snapshot(T1230 + 8 * SEC).get("decision_history", []), [])


class DashboardTests(Base):
    def state(self, confirmations=None, ctx=None, auto=False):
        s = LiveSession(ctx or adaptive_ctx(confirmations), os.path.join(self.dir, "trader"), J.Journal(self.path, "fixture", "d"), DAY,
                        auto_entry=auto)
        s.step(T1230 + 7 * SEC, sc.scenario("sideways"))
        view = s.snapshot(T1230 + 8 * SEC)
        view["mode"], view["written_at"] = "LIVE", datetime.datetime.now(datetime.timezone.utc).isoformat()
        path = os.path.join(self.dir, "state.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(view, fh, default=str)
        return path, view

    @staticmethod
    def text(html):
        import re
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    def test_the_page_shows_readiness_checklist_entry_strategies_and_history(self):
        path, view = self.state()
        t = self.text(dashboard.live_fragment(path))
        for needle in ("Trade Readiness", "WAITING FOR CONFIRMATION", "Checklist: Iron Condor", "PASSED", "PENDING",
                       "Expected entry", "Entry trigger", "Stop-loss", "Target", "Reward-to-risk", "All four strategies",
                       "Directional Debit Spread", "Iron Butterfly", "Decision history", "Later valid?", "Next:",
                       "no time or price is promised", "volume confirmation", "no traded volume"):
            self.assertIn(needle, t)
        self.assertNotIn("READY TO EXECUTE", t)                                       # nothing has passed the risk gate

    def test_an_executed_trade_is_labelled_trade_executed(self):
        path, view = self.state(confirmations=1, auto=True)
        self.assertIn("TRADE EXECUTED", self.text(dashboard.live_fragment(path)))

    def test_a_manual_mode_pick_that_passed_the_risk_gate_shows_ready_to_execute_and_says_it_waits_for_a_yes(self):
        path, view = self.state(confirmations=1, auto=False)
        t = self.text(dashboard.live_fragment(path))
        self.assertIn("READY TO EXECUTE", t)
        self.assertIn("Waiting for your", t)

    def test_when_nothing_blocks_trading_but_no_structure_exists_it_says_entry_not_available(self):
        ctx = adaptive_ctx()
        ctx = ctx._replace(strategy_params=ctx.strategy_params._replace(min_days_to_expiry=30))
        path, view = self.state(ctx=ctx)
        t = self.text(dashboard.live_fragment(path))
        for needle in ("EXECUTION BLOCKED", "ENTRY NOT AVAILABLE", "no listed expiry is at least 30 days away", "EXPIRY",
                       "data, expiry and safety gates", "ERROR"):
            self.assertIn(needle, t)
        self.assertNotIn("Priced at", t)                                              # no price is shown without a structure

    def test_nothing_is_rendered_without_a_readiness_and_input_is_escaped(self):
        self.assertEqual(dashboard._readiness_card(None), "")
        self.assertEqual(dashboard._history_card([]), "")
        evil = {"status": "WAITING FOR SIGNAL", "regime": {"structure": "<b>x</b>", "summary": "<script>1</script>", "as_of": "2026-11-05T10:00:00+05:30"},
                "evaluated_strategy": "<i>s</i>", "direction": "NEUTRAL", "counts": {}, "checklist": [
                    {"status": "FAILED", "name": "<img src=x>", "detail": "<script>", "required": True}],
                "entry": {"available": False, "label": "ENTRY NOT AVAILABLE", "reason": "<script>alert(1)</script>"},
                "next_action": "<u>go</u>", "strategies": [], "technical": [{"kind": "<k>", "text": "<t>"}]}
        html = dashboard._readiness_card(evil)
        for tag in ("<script", "<img", "<i>", "<u>", "<k>", "<t>"):
            self.assertNotIn(tag, html)

    def test_no_text_on_the_page_is_escaped_twice(self):
        """A screenshot once showed 'P&AMP;L': a label that was escaped by the caller and again by the component."""
        for confirmations, auto in ((None, False), (1, True)):
            path, view = self.state(confirmations=confirmations, auto=auto)
            html = dashboard.live_fragment(path)
            self.assertNotIn("&amp;amp;", html)
            self.assertNotIn("&amp;lt;", html)
            self.assertNotIn("&amp;#x27;", html)
            self.assertIn("Day P&amp;L", html)

    def test_the_page_has_no_controls(self):
        path, view = self.state()
        html = dashboard.live_fragment(path)
        for control in ("<form", "<button", "<input"):
            self.assertNotIn(control, html)


class ApiTests(Base):
    def state_path(self):
        return DashboardTests.state(self)[0]

    def test_the_readiness_endpoint_returns_the_structured_decision(self):
        status, body = dashboard.api_route("/api/trade-readiness", self.state_path())
        doc = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual((doc["kind"], doc["paper"], doc["day"]), ("TRADE_READINESS", True, str(DAY)))
        self.assertEqual(doc["readiness"]["status"], RD.WAITING_CONFIRMATION)
        self.assertEqual(len(doc["readiness"]["strategies"]), 4)
        self.assertIn("checklist", doc["readiness"])
        self.assertIn("PASSED", doc["legend"])

    def test_the_history_endpoint_returns_the_days_decisions(self):
        status, body = dashboard.api_route("/api/decision-history", self.state_path())
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["decision_history"]), 1)

    def test_unknown_endpoints_and_a_missing_session_are_404_json(self):
        self.assertEqual(dashboard.api_route("/api/other", self.state_path())[0], 404)
        status, body = dashboard.api_route("/api/trade-readiness", os.path.join(self.dir, "missing.json"))
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_the_handler_serves_the_api_as_json_and_the_pages_as_html_and_stays_get_only(self):
        path = self.state_path()
        handler_cls = dashboard.make_handler(self.dir, path)
        self.assertIs(handler_cls.do_POST, handler_cls._refuse)

        def get(url):
            h = object.__new__(handler_cls)
            sent = {"headers": {}}
            h.path, h.headers, h.wfile = url, {}, io.BytesIO()
            h.send_response = lambda code, *a: sent.__setitem__("code", code)
            h.send_header = lambda k, v: sent["headers"].__setitem__(k, v)
            h.end_headers = lambda: None
            h.do_GET()
            return sent["code"], sent["headers"]["Content-Type"], h.wfile.getvalue().decode("utf-8")
        code, ctype, body = get("/api/trade-readiness")
        self.assertEqual((code, ctype.split(";")[0]), (200, "application/json"))
        self.assertEqual(json.loads(body)["kind"], "TRADE_READINESS")
        code, ctype, body = get("/live?fragment=1")
        self.assertEqual((code, ctype.split(";")[0]), (200, "text/html"))
        self.assertIn("Trade Readiness", body)


class ExpiryChoiceTests(NoNetworkTestCase):
    def test_live_run_asks_for_an_expiry_at_least_as_far_as_the_strategys_minimum(self):
        import inspect
        src = inspect.getsource(live_run.run_live)
        self.assertIn("min_days=ctx.strategy_params.min_days_to_expiry", src)


if __name__ == "__main__":
    unittest.main()
