"""The live PAPER session: tickets wait for a reply, an entry needs a fresh "yes", unrealised P&L shows at once.

Driven offline on a fixture with a simulated clock: no network, no real token."""
import datetime
import json
import os
import tempfile
import unittest
from decimal import Decimal as Dec

from nifty_core import pipeline as P
from nifty_core.journal import journal as J
from nifty_core.journal import report as REP
from nifty_core.live_session import LiveSession
from nifty_live import live_run
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase
from tests.integration.test_pipeline import big_cfg
from tools import dashboard

SEC = datetime.timedelta(seconds=1)


class LiveBase(NoNetworkTestCase):
    def setUp(self):
        super(LiveBase, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.scenario = sc.scenario("bullish_trend")
        self.ctx = P.load_context(big_cfg())
        self.path = os.path.join(self.dir, "live.jsonl")
        self.session = LiveSession(self.ctx, os.path.join(self.dir, "trader"),
                                   J.Journal(self.path, "fixture", "live-test"), self.scenario.day)
        self.times = live_run.replay_times(self.scenario, self.ctx, self.scenario.day)

    def kinds(self, kind):
        return [r for r in J.read_records(self.path) if r["kind"] == kind]

    def statuses(self):
        return [r["payload"]["status"] for r in self.kinds("DECISION")]

    def run_until_ticket(self):
        """Step the simulated clock until a ticket is waiting. Returns (its time, its fingerprint)."""
        for now in self.times:
            self.session.step(now, self.scenario)
            if self.session.runner.pending:
                return now, next(iter(self.session.runner.pending))
        self.fail("no ticket appeared")


class TicketTests(LiveBase):
    def test_an_unanswered_ticket_waits_then_is_released_and_nothing_is_entered(self):
        now, fp = self.run_until_ticket()
        view = self.session.snapshot(now)
        self.assertEqual([t["fingerprint"] for t in view["pending_tickets"]], [fp])
        self.assertEqual((view["last_decision"]["status"], view["totals"]["trades_today"]), ("AWAITING_APPROVAL", 0))
        self.assertGreater(self.session.runner.mgr.snapshot().open_risk_reserved_inr, 0)   # risk is held while it waits
        self.session.step(now + 30 * SEC, self.scenario)
        self.assertIn(fp, self.session.runner.pending)                       # 30 s: still fresh
        self.session.step(now + 90 * SEC, self.scenario)                     # 90 s: past the 60 s approval window
        self.assertEqual(self.session.runner.pending, {})
        self.assertEqual(self.session.runner.mgr.snapshot().open_risk_reserved_inr, 0)
        self.assertIn("TICKET_EXPIRED", self.statuses())
        self.assertEqual(self.session.snapshot(now + 95 * SEC)["last_decision"]["status"], "TICKET_EXPIRED")
        self.assertEqual(self.kinds("EXECUTION"), [])
        self.assertEqual(self.session.answer(fp, "yes", now + 95 * SEC), "UNKNOWN_TICKET")
        self.assertEqual(self.kinds("EXECUTION"), [])
        late = self.kinds("APPROVAL")[-1]["payload"]
        self.assertEqual((late["accepted"], late["reply_verbatim"], late["source"]), (False, "yes", "live_answer"))

    def test_the_next_decision_time_releases_a_ticket_nobody_answered(self):
        now, fp = self.run_until_ticket()
        self.session.step(now + datetime.timedelta(minutes=5), self.scenario)
        self.assertNotIn(fp, self.session.runner.pending)
        self.assertEqual(self.session.runner.mgr.snapshot().open_risk_reserved_inr, 0)

    def test_a_reply_that_is_not_a_yes_enters_nothing(self):
        now, fp = self.run_until_ticket()
        self.assertEqual(self.session.answer(fp, "ok go", now + 5 * SEC), "DECLINED")
        self.assertEqual(self.kinds("EXECUTION"), [])
        self.assertEqual(self.session.runner.mgr.snapshot().open_risk_reserved_inr, 0)
        self.assertFalse(self.kinds("APPROVAL")[-1]["payload"]["accepted"])
        self.assertEqual(self.session.answer(fp, "yes", now + 6 * SEC), "UNKNOWN_TICKET")   # one ticket, one reply

    def test_a_reply_for_another_ticket_enters_nothing(self):
        now, fp = self.run_until_ticket()
        self.assertEqual(self.session.answer("0123456789abcdef", "yes", now + 5 * SEC), "UNKNOWN_TICKET")
        self.assertEqual(self.session.runner.pending.keys(), {fp})
        self.assertEqual(self.kinds("EXECUTION"), [])

    def test_a_yes_that_arrives_after_the_approval_went_stale_is_refused_by_the_executor(self):
        now, fp = self.run_until_ticket()
        status = self.session.answer(fp, "yes", now + 120 * SEC)            # nobody stepped the clock in between
        self.assertEqual(status, "ENTRY_NOT_FILLED")
        self.assertEqual(self.kinds("EXECUTION")[0]["payload"]["status"], "REJECTED")
        self.assertIn("APPROVAL_STALE", [r["code"] for r in self.kinds("EXECUTION")[0]["payload"]["reasons"]])
        self.assertEqual(self.session.runner.pm.active(), ())
        self.assertEqual(self.session.runner.mgr.snapshot().open_risk_reserved_inr, 0)

    def test_the_kill_switch_stops_a_live_entry(self):
        now, fp = self.run_until_ticket()
        os.makedirs(os.path.join(self.dir, "trader"))
        open(os.path.join(self.dir, "trader", "KILL"), "w").close()
        self.assertEqual(self.session.answer(fp, "yes", now + 5 * SEC), "ENTRY_NOT_FILLED")
        self.assertEqual(self.session.runner.pm.active(), ())
        self.assertTrue(self.session.snapshot(now + 6 * SEC)["kill_switch"])


class EntryAndMarkTests(LiveBase):
    def enter(self):
        now, fp = self.run_until_ticket()
        status = self.session.answer(fp, "yes", now + 5 * SEC)
        self.assertEqual(status, "ENTERED")
        return now + 5 * SEC, fp

    def test_unrealised_pnl_is_shown_the_moment_a_position_is_open(self):
        at, fp = self.enter()
        view = self.session.snapshot(at)
        pos = view["open_positions"][0]
        u = pos["unrealized"]
        self.assertIsNotNone(u)
        self.assertIn("UNREALIZED", u["label"])
        # fills are adverse and costs are paid, so the first mark is a small loss, never a gain
        self.assertLess(Dec(u["net_pnl_inr"]), 0)
        self.assertEqual(Dec(view["totals"]["unrealised_net_inr"]), Dec(u["net_pnl_inr"]))
        self.assertEqual(Dec(view["totals"]["day_total_inr"]), Dec(u["net_pnl_inr"]))
        self.assertEqual(view["totals"]["realised_net_inr"], "0.00")
        self.assertEqual(len(pos["legs"]), 2)
        self.assertEqual({leg["side"] for leg in pos["legs"]}, {"BUY", "SELL"})
        for leg in pos["legs"]:
            self.assertIsNotNone(leg["mark"])
        self.assertIn("UNREALISED", view["note"])
        self.assertEqual((view["last_decision"]["status"], view["totals"]["trades_today"]), ("ENTERED", 1))
        self.assertLess(Dec(pos["stop_at_net_inr"]), 0)
        self.assertGreater(Dec(pos["target_at_net_inr"]), 0)
        self.assertEqual(view["pending_tickets"], [])

    def test_leg_pnl_adds_up_to_the_gross_figure(self):
        at, _ = self.enter()
        pos = self.session.snapshot(at)["open_positions"][0]
        self.assertEqual(sum((Dec(l["leg_gross_inr"]) for l in pos["legs"]), Dec(0)), Dec(pos["unrealized"]["gross_pnl_inr"]))

    def test_marks_are_journaled_and_the_journal_still_verifies(self):
        at, _ = self.enter()
        self.session._journal_marks(at + 10 * SEC)
        marks = self.kinds("MARK")
        self.assertEqual(len(marks), 1)
        self.assertIn("UNREALIZED", marks[0]["payload"]["unrealized"]["label"])
        self.assertTrue(J.verify(self.path).ok)

    def test_the_mark_goes_blank_when_quotes_are_stale_instead_of_inventing_a_price(self):
        at, _ = self.enter()
        view = self.session.snapshot(at + datetime.timedelta(minutes=3))
        pos = view["open_positions"][0]
        self.assertIsNone(pos["unrealized"])
        self.assertTrue(pos["mark_problems"])
        self.assertIsNone(view["totals"]["unrealised_net_inr"])
        self.assertIsNone(view["totals"]["day_total_inr"])

    def test_the_whole_day_ends_with_a_closed_trade_a_valid_journal_and_no_open_position(self):
        at, _ = self.enter()
        for now in self.times:
            if now > at:
                self.session.step(now, self.scenario)
        summary = self.session.finish(self.times[-1])
        self.assertEqual((summary.entries, summary.exits, summary.criticals), (1, 1, 0))
        view = self.session.snapshot(self.times[-1])
        self.assertEqual(view["status"], "FINISHED")
        self.assertEqual(view["open_positions"], [])
        self.assertEqual(len(view["closed_trades"]), 1)
        self.assertEqual(view["totals"]["realised_net_inr"], str(Dec(view["closed_trades"][0]["net_pnl_inr"])))
        self.assertTrue(J.verify(self.path).ok)
        rep = REP.build_report(self.path)                       # existing reports keep working on a live journal
        self.assertTrue(rep["journal_ok"])
        self.assertEqual(rep["completed_trades"], 1)
        self.assertGreaterEqual(len(view["curve"]), 10)

    def test_a_second_position_cannot_be_opened_while_one_is_open(self):
        at, _ = self.enter()
        for now in self.times:
            if at < now <= at + datetime.timedelta(minutes=20):
                self.session.step(now, self.scenario)
                self.assertLessEqual(len(self.session.runner.pm.active()), 1)
                self.assertEqual(self.session.runner.pending, {})


class FilesTests(NoNetworkTestCase):
    def setUp(self):
        super(FilesTests, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_state_file_is_replaced_whole_and_reads_back(self):
        path = os.path.join(self.dir, "live", "state.json")
        self.assertTrue(live_run.write_state({"kind": "LIVE_PAPER_STATE", "x": Dec("1.5")}, path))
        self.assertTrue(live_run.write_state({"kind": "LIVE_PAPER_STATE", "x": 2}, path))
        self.assertEqual(live_run.read_state(path), {"kind": "LIVE_PAPER_STATE", "x": 2})
        self.assertIsNone(live_run.read_state(os.path.join(self.dir, "missing.json")))

    def test_answer_command_appends_the_reply_word_for_word_and_the_inbox_reads_it_once(self):
        answers = os.path.join(self.dir, "answers.jsonl")
        with open(answers, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"fingerprint": "f" * 16, "reply": "yes"}) + "\n")      # from an earlier run
        inbox = live_run.AnswerInbox(answers)
        self.assertEqual(inbox.new(), [])
        state = os.path.join(self.dir, "state.json")
        self.assertEqual(live_run.cmd_answer(["0123456789ABCDEF", "yes", "--answers-file", answers,
                                              "--state-file", state]), 0)
        self.assertEqual(inbox.new(), [("0123456789abcdef", "yes")])
        self.assertEqual(inbox.new(), [])
        live_run.cmd_answer(["0123456789abcdef", "yes", "but", "wait", "--answers-file", answers, "--state-file", state])
        self.assertEqual(inbox.new(), [("0123456789abcdef", "yes but wait")])

    def test_answer_command_rejects_a_malformed_fingerprint(self):
        with self.assertRaises(SystemExit):
            live_run.cmd_answer(["not-a-fingerprint", "yes", "--answers-file", os.path.join(self.dir, "a.jsonl")])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "a.jsonl")))

    def test_live_mode_refuses_a_fixture_config(self):
        args = live_run.build_parser().parse_args(["--config", live_run.DEFAULT_CONFIG])
        with self.assertRaises(SystemExit) as cm:
            live_run.run_live(args)
        self.assertIn("real_readonly", str(cm.exception))


class ReplayRunnerTests(LiveBase):
    def test_runner_publishes_state_prints_the_ticket_and_applies_a_reply(self):
        state = os.path.join(self.dir, "state.json")
        out = []
        inbox = live_run.AnswerInbox(os.path.join(self.dir, "answers.jsonl"))
        runner = live_run.Runner(self.ctx, self.scenario.day, J.Journal(os.path.join(self.dir, "r.jsonl"), "fixture", "r"),
                                 "REPLAY", state, inbox, out.append)
        for now in self.times:
            runner.step(now, self.scenario)
            if runner.session.runner.pending:
                break
        fp = next(iter(runner.session.runner.pending))
        self.assertTrue(any(fp in line and "answer" in line for line in out))          # the command is printed
        self.assertEqual([t["fingerprint"] for t in live_run.read_state(state)["pending_tickets"]], [fp])
        live_run.cmd_answer([fp, "yes", "--answers-file", inbox.path, "--state-file", state])
        runner.apply_replies(now + 5 * SEC)
        saved = live_run.read_state(state)
        self.assertEqual(saved["pending_tickets"], [])
        self.assertEqual(len(saved["open_positions"]), 1)
        self.assertEqual(saved["mode"], "REPLAY")
        self.assertIn("written_at", saved)


class LivePageTests(LiveBase):
    def page(self, view, now=None):
        path = os.path.join(self.dir, "state.json")
        live_run.write_state(dict(view, mode="LIVE", written_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                  problem=None), path)
        return dashboard.render_live(path, self.dir, now)

    def assert_read_only(self, text, scripts=1):
        """No control of any kind. The only script allowed is the one that re-fetches the page fragment with a GET."""
        for control in ("<form", "<button", "<input", "<textarea", "<select"):
            self.assertNotIn(control, text)
        self.assertEqual(text.count("<script"), scripts)
        if scripts:
            script = text[text.index("<script"):text.index("</script>")]
            self.assertIn("fetch('/live?fragment=1'", script)
            for word in ("POST", "PUT", "DELETE", "method", "body:", "XMLHttpRequest", "WebSocket", "eval("):
                self.assertNotIn(word, script)

    def test_page_shows_the_waiting_ticket_with_its_command_and_cannot_act(self):
        now, fp = self.run_until_ticket()
        text = self.page(self.session.snapshot(now))
        self.assertIn("Ticket waiting for your reply", text)
        self.assertIn("python -m nifty_live.live_run answer %s yes" % fp, text)
        self.assertIn("cannot approve anything", text)
        self.assert_read_only(text)

    def test_page_shows_the_open_position_and_unrealised_pnl_with_its_caveat(self):
        now, fp = self.run_until_ticket()
        self.session.answer(fp, "yes", now + 5 * SEC)
        text = self.page(self.session.snapshot(now + 5 * SEC))
        self.assertIn("UNREALISED net P&amp;L (simulated)", text)
        self.assertIn("not evidence of profitability", text)
        self.assertIn("exit mark", text)
        self.assertIn("<svg", text)
        self.assertIn('http-equiv="refresh"', text)
        self.assert_read_only(text)

    def test_page_without_a_session_says_how_to_start_one(self):
        text = dashboard.render_live(os.path.join(self.dir, "nothing.json"))
        self.assertIn("live_run", text)
        status, _ = dashboard.route(self.dir, "/live", os.path.join(self.dir, "nothing.json"))
        self.assertEqual(status, 200)

    def test_a_runner_that_stopped_writing_is_flagged(self):
        self.session.step(self.times[0], self.scenario)
        path = os.path.join(self.dir, "state.json")
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=10)).isoformat()
        live_run.write_state(dict(self.session.snapshot(self.times[0]), mode="LIVE", written_at=old, problem=None), path)
        self.assertIn("NOT UPDATING", dashboard.render_live(path, self.dir))
        live_run.write_state(dict(self.session.snapshot(self.times[0]), mode="REPLAY", written_at=old, problem=None), path)
        self.assertNotIn("NOT UPDATING", dashboard.render_live(path, self.dir))

    def test_hostile_text_in_the_state_file_is_escaped_and_a_bad_fingerprint_gives_no_command(self):
        bad = "<script>alert(1)</script>"
        view = dict(self.session.snapshot(self.times[0]), note=bad, label=bad,
                    pending_tickets=[{"fingerprint": bad, "text": bad, "decided_at": "x", "expires_at": "x"}],
                    events=[{"at": "x", "text": bad}])
        text = self.page(view)
        self.assertNotIn("<script>alert(1)", text)
        self.assertEqual(text.count("<script"), 1)                      # only our own fetch script
        self.assertIn("&lt;script&gt;alert(1)", text)
        self.assertIn("(fingerprint missing)", text)

    def test_a_broken_or_foreign_state_file_is_ignored(self):
        path = os.path.join(self.dir, "state.json")
        for content in ("not json", '{"kind": "SOMETHING_ELSE"}', "[]"):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self.assertIsNone(dashboard.load_live_state(path))

    def test_live_route_only_answers_get(self):
        handler = dashboard.make_handler(self.dir, os.path.join(self.dir, "state.json"))
        for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
            self.assertIs(getattr(handler, verb), handler._refuse)


class LiveDataTests(LiveBase):
    """The page moves with the data: spot series, levels, option chain, fragment updates."""
    assert_read_only = LivePageTests.assert_read_only

    def test_the_session_keeps_every_new_snapshot_spot_and_a_chain_window_around_the_money(self):
        for now in self.times[:6]:
            self.session.step(now, self.scenario)
        view = self.session.snapshot(self.times[5])
        self.assertGreaterEqual(len(view["spot_series"]), 1)
        stamps = [p[0] for p in view["spot_series"]]
        self.assertEqual(stamps, sorted(set(stamps)))                    # one point per snapshot, in order
        rows = view["chain_view"]["rows"]
        self.assertEqual(len(rows), 11)                                   # 5 strikes each side plus the money
        self.assertEqual(sum(1 for r in rows if r["atm"]), 1)
        spot = Dec(view["market"]["spot"])
        atm = [r for r in rows if r["atm"]][0]
        self.assertEqual(Dec(atm["strike"]), min((Dec(r["strike"]) for r in rows), key=lambda k: abs(k - spot)))
        self.assertTrue(view["candle_closes"])

    def test_open_legs_are_marked_in_the_chain_window(self):
        now, fp = self.run_until_ticket()
        self.session.answer(fp, "yes", now + 5 * SEC)
        view = self.session.snapshot(now + 5 * SEC)
        marks = [(r["strike"], k, v) for r in view["chain_view"]["rows"] for k, v in r["held"].items() if v]
        self.assertEqual(sorted(m[2] for m in marks), ["BUY", "SELL"])
        text = dashboard.live_fragment(self.write_state(view, now), self.dir)
        self.assertIn("class=tag>BUY", text.replace("'", ""))
        self.assertIn("Option chain around the money", text)

    def write_state(self, view, now):
        path = os.path.join(self.dir, "fragment_state.json")
        live_run.write_state(dict(view, mode="LIVE", written_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                                  problem=None, poll_seconds=10), path)
        return path

    def test_levels_exist_only_for_a_profile_with_a_setup(self):
        for now in self.times[:6]:
            self.session.step(now, self.scenario)
        self.assertIsNone(self.session.snapshot(self.times[5])["levels"])         # the strict profile has no setup
        ctx = P.load_context(big_cfg()._replace(entry_window_start_ist="09:45"), "fixture", "intraday")
        session = LiveSession(ctx, os.path.join(self.dir, "trader"), J.Journal(os.path.join(self.dir, "i.jsonl"), "fixture", "i"),
                              self.scenario.day)
        for now in live_run.replay_times(self.scenario, ctx, self.scenario.day)[:20]:
            session.step(now, self.scenario)
            last = now
        lv = session.snapshot(last)["levels"]
        self.assertIsNotNone(lv)
        self.assertLess(Dec(lv["opening_range_low"]), Dec(lv["opening_range_high"]))
        self.assertGreater(Dec(lv["breakout_up"]), Dec(lv["opening_range_high"]))
        self.assertLess(Dec(lv["breakout_down"]), Dec(lv["opening_range_low"]))

    def test_the_fragment_has_no_script_and_the_full_page_wraps_it_with_the_one_fetch_script(self):
        for now in self.times[:8]:
            self.session.step(now, self.scenario)
        path = self.write_state(self.session.snapshot(self.times[7]), self.times[7])
        fragment = dashboard.live_fragment(path, self.dir)
        self.assert_read_only(fragment, scripts=0)
        self.assertIn("page updates every second", fragment)
        self.assertIn("polling every 10 s", fragment)
        full = dashboard.render_live(path, self.dir)
        self.assert_read_only(full)
        self.assertIn("<div id=live>", full)
        self.assertIn('<noscript><meta http-equiv="refresh"', full)         # still updates, slowly, with scripts off
        status, via_route = dashboard.route(self.dir, "/live?fragment=1", path)
        self.assertEqual((status, via_route), (200, fragment))
        self.assertIn("NIFTY today", fragment)

    def test_the_price_chart_draws_the_levels_and_needs_two_points(self):
        self.assertEqual(dashboard.spot_svg([["2026-11-05T09:30:00+05:30", "24400"]], [], None), "")
        svg = dashboard.spot_svg([["2026-11-05T09:30:00+05:30", "24400"], ["2026-11-05T09:30:10+05:30", "24410"]],
                                 [["2026-11-05T09:25:00+05:30", "24390"]],
                                 {"opening_range_high": "24420", "opening_range_low": "24300", "breakout_up": "24427",
                                  "breakout_down": "24293", "day_average": "24380"})
        for word in ("range high", "range low", "long trigger", "short trigger", "day avg", "<polyline", "<circle"):
            self.assertIn(word, svg)
        self.assertNotIn("<script", svg)

    def test_every_chart_is_well_formed_for_html(self):
        """An unquoted attribute value right before '/>' swallows the slash, so the element is never closed and everything
        after it is nested inside it and not drawn. No chart may contain that."""
        import re
        series = [["2026-11-05T09:30:%02d+05:30" % i, str(24400 + i)] for i in range(0, 30, 10)]
        charts = [dashboard.spot_svg(series, [["2026-11-05T09:25:00+05:30", "24390"]],
                                     {"opening_range_high": "24420", "opening_range_low": "24300", "breakout_up": "24427",
                                      "breakout_down": "24293", "day_average": "24380"}),
                  dashboard.gauge_svg("100", "-500", "800"),
                  dashboard.equity_svg([("a", Dec(0)), ("b", Dec(5)), ("c", Dec(-3))])]
        for svg in charts:
            self.assertTrue(svg.startswith("<svg"))
            self.assertIsNone(re.search(r"=[^'\" >]+/>", svg), svg[:200])
            self.assertEqual(svg.count("<svg"), svg.count("</svg>"))

    def test_a_dead_runner_still_shows_the_red_dot_and_a_replay_never_does(self):
        self.session.step(self.times[0], self.scenario)
        path = os.path.join(self.dir, "old.json")
        old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)).isoformat()
        live_run.write_state(dict(self.session.snapshot(self.times[0]), mode="LIVE", written_at=old, problem=None,
                                  poll_seconds=10), path)
        self.assertIn("dot off", dashboard.live_fragment(path, self.dir))
        live_run.write_state(dict(self.session.snapshot(self.times[0]), mode="REPLAY", written_at=old, problem=None), path)
        self.assertNotIn("dot off", dashboard.live_fragment(path, self.dir))


class LivePollingTests(NoNetworkTestCase):
    def test_chain_documents_built_in_memory_match_the_csv_conversion_exactly(self):
        import csv as csvmod
        from nifty_live import recorder
        from tests.unit.test_live_recorder import CHAIN, FakeTransport, TOKEN
        from tools import csv_to_scenario
        t = datetime.datetime(2026, 11, 5, 10, 15, 7, tzinfo=datetime.timezone(datetime.timedelta(hours=5, minutes=30)))
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        got = []
        recorder.record_snapshot(TOKEN, d, t, "current_week", 1000, FakeTransport(),
                                 sink=lambda stamp, spot, rows: got.append(live_run.chain_doc(stamp, spot, rows)))
        with open(os.path.join(d, "candles.csv"), "w", encoding="utf-8") as fh:
            fh.write(",".join(recorder.CANDLE_FIELDS) + "\n")
        via_csv = csv_to_scenario.build("2026-11-05", "x", os.path.join(d, "candles.csv"), os.path.join(d, "chain.csv"))
        self.assertEqual(got, via_csv["chains"])
        self.assertEqual(live_run.read_candles(d), via_csv["candles"])

    def test_the_live_scenario_is_built_from_memory_and_keeps_only_the_newest_snapshots(self):
        scenario = sc.scenario("bullish_trend")
        ctx = P.load_context(big_cfg())
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        with open(os.path.join(d, "candles.csv"), "w", encoding="utf-8") as fh:
            fh.write("start,open,high,low,close,volume\n")
        recent = [live_run.chain_doc(c.snapshot_time.isoformat(), str(c.spot), [
            {"expiry": q.contract.expiry.isoformat(), "strike": int(q.contract.strike), "type": q.contract.option_type,
             "bid": str(q.bid or ""), "ask": str(q.ask or ""), "ltp": str(q.ltp or ""), "bid_qty": str(q.bid_qty or ""),
             "ask_qty": str(q.ask_qty or ""), "volume": str(q.volume or ""), "open_interest": str(q.open_interest or "")}
            for q in c.quotes[:20]]) for c in scenario.chains]
        built = live_run.build_live_scenario(d, scenario.day, ctx._replace(data_mode="fixture"), recent)
        self.assertLessEqual(len(built.chains), live_run.KEEP_CHAINS)
        self.assertEqual(built.chains[-1].snapshot_time, scenario.chains[-1].snapshot_time)

    def test_live_polling_defaults_to_ten_seconds_and_never_below_five(self):
        a = live_run.build_parser().parse_args([])
        self.assertEqual(a.interval, 10)
        self.assertEqual(live_run.LIVE_MIN_POLL_SECONDS, 5)


if __name__ == "__main__":
    unittest.main()
