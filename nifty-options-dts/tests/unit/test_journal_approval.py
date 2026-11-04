"""Journal (append-only, hash-chained), reports, and the approval ticket / reply parser."""
import datetime
import json
import os
import re
import tempfile
import unittest
from decimal import Decimal as Dec

from nifty_core import approval as A
from nifty_core.errors import JournalError
from nifty_core.journal import journal as J
from nifty_core.journal import report as REP
from nifty_core.market.ist import parse_ist
from nifty_core.risk import engine as R
from nifty_core.risk import ledger as L
from tests import _scenarios as sc
from tests._base import NoNetworkTestCase
from tests.unit import test_risk as TR

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
T0 = parse_ist("2026-09-16 12:30")


def _read(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
        return fh.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TmpCase(NoNetworkTestCase):
    def setUp(self):
        super(TmpCase, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.path = os.path.join(self.dir, "sub", "j.jsonl")


class JournalTests(TmpCase):
    def test_records_are_appended_in_order_with_a_hash_chain(self):
        j = J.Journal(self.path, "fixture", "r1")
        a = j.append("RUN_START", T0, {"x": 1})
        b = j.append("DECISION", T0 + datetime.timedelta(minutes=5), {"status": "NO_TRADE"})
        self.assertEqual((a["seq"], b["seq"]), (1, 2))
        self.assertEqual(a["prev_hash"], J.GENESIS)
        self.assertEqual(b["prev_hash"], a["hash"])
        self.assertEqual(a["at"], "2026-09-16T12:30:00+05:30")
        self.assertIs(a["paper"], True)
        res = J.verify(self.path)
        self.assertEqual((res.ok, res.records, res.problems), (True, 2, []))

    def test_appending_never_changes_earlier_bytes(self):
        j = J.Journal(self.path, "fixture", "r1")
        j.append("RUN_START", T0, {})
        before = _read(self.path, "rb")
        j.append("DECISION", T0, {"status": "NO_TRADE"})
        after = _read(self.path, "rb")
        self.assertTrue(after.startswith(before))
        self.assertGreater(len(after), len(before))

    def test_a_second_journal_object_continues_the_same_chain(self):
        J.Journal(self.path, "fixture", "r1").append("RUN_START", T0, {})
        j2 = J.Journal(self.path, "fixture", "r2")
        rec = j2.append("DECISION", T0, {"status": "NO_TRADE"})
        self.assertEqual(rec["seq"], 2)
        self.assertTrue(J.verify(self.path).ok)

    def test_an_edit_made_outside_the_tool_is_detected(self):
        j = J.Journal(self.path, "fixture", "r1")
        for i in range(3):
            j.append("DECISION", T0, {"status": "NO_TRADE", "n": i})
        _write(self.path, _read(self.path).replace('"n":1', '"n":9'))              # tampering, done by the test
        res = J.verify(self.path)
        self.assertFalse(res.ok)
        self.assertTrue([p for p in res.problems if "changed after" in p])
        with self.assertRaises(JournalError) as cm:
            J.Journal(self.path, "fixture", "r2")
        self.assertEqual(cm.exception.code, "DAMAGED")

    def test_a_deleted_or_reordered_record_breaks_the_chain(self):
        j = J.Journal(self.path, "fixture", "r1")
        for i in range(3):
            j.append("DECISION", T0, {"n": i})
        lines = open(self.path, encoding="utf-8").read().splitlines()
        _write(self.path, "\n".join([lines[0], lines[2]]) + "\n")
        res = J.verify(self.path)
        self.assertFalse(res.ok)
        self.assertTrue([p for p in res.problems if "chain broken" in p or "seq" in p])

    def test_each_check_in_verify_stands_on_its_own(self):
        j = J.Journal(self.path, "fixture", "r1")
        for i in range(3):
            j.append("DECISION", T0, {"n": i})
        recs = J.read_records(self.path)

        def rewrite(records):
            _write(self.path, "".join(J._canonical(r) + chr(10) for r in records))

        def rehash(rec):
            body = {k: v for k, v in rec.items() if k != "hash"}
            return dict(body, hash=J._hash(body))
        # 1: wrong prev_hash, but seq and the record's own hash are consistent
        bad = [recs[0], rehash(dict(recs[1], prev_hash="f" * 64)), recs[2]]
        rewrite(bad)
        self.assertTrue([p for p in J.verify(self.path).problems if "chain broken" in p])
        # 2: wrong seq, consistent hash and chain
        bad = [recs[0], rehash(dict(recs[1], seq=7)), recs[2]]
        rewrite(bad)
        self.assertTrue([p for p in J.verify(self.path).problems if "seq" in p])
        # 3: wrong own hash only
        bad = [recs[0], dict(recs[1], hash="e" * 64), recs[2]]
        rewrite(bad)
        self.assertTrue([p for p in J.verify(self.path).problems if "changed after" in p])

    def test_fixture_and_real_data_are_never_mixed_in_one_file(self):
        J.Journal(self.path, "fixture", "r1").append("RUN_START", T0, {})
        with self.assertRaises(JournalError) as cm:
            J.Journal(self.path, "real", "r2")
        self.assertEqual(cm.exception.code, "MIXED_DATA_SOURCES")
        with self.assertRaises(JournalError):
            J.Journal(os.path.join(self.dir, "x.jsonl"), "synthetic", "r")

    def test_unknown_record_kinds_are_refused(self):
        j = J.Journal(self.path, "fixture", "r1")
        with self.assertRaises(JournalError) as cm:
            j.append("REWRITE", T0, {})
        self.assertEqual(cm.exception.code, "BAD_KIND")

    def test_the_journal_has_no_rewrite_delete_or_truncate_operation(self):
        public = [n for n in dir(J.Journal) if not n.startswith("_")]
        self.assertEqual(sorted(public), ["append"])
        with open(os.path.join(ROOT, "nifty_core", "journal", "journal.py"), encoding="utf-8") as fh:
            src = fh.read()
        for pattern in (r"open\([^)]*['\"]w", r"open\([^)]*['\"]r\+", r"\.truncate\(", r"os\.remove", r"os\.unlink",
                        r"os\.rename", r"os\.replace", r"shutil"):
            self.assertIsNone(re.search(pattern, src), pattern)

    def test_time_comes_from_the_decision_not_the_machine_clock(self):
        a = J.Journal(self.path, "fixture", "r1").append("RUN_START", T0, {"a": 1})
        b = J.Journal(os.path.join(self.dir, "other.jsonl"), "fixture", "r1").append("RUN_START", T0, {"a": 1})
        self.assertEqual(a["hash"], b["hash"])                       # identical inputs, identical bytes

    def test_data_source_from_label(self):
        self.assertEqual(J.data_source_for_label("FIXTURE_SYNTHETIC"), "fixture")
        self.assertEqual(J.data_source_for_label("RECORDED_REAL"), "real")
        self.assertEqual(J.data_source_for_label("LIVE_READONLY"), "real")

    def test_bad_json_line_is_reported(self):
        os.makedirs(os.path.dirname(self.path))
        _write(self.path, "not json\n")
        self.assertFalse(J.verify(self.path).ok)


def exit_record(j, pnl, reason="PROFIT_TARGET"):
    j.append("EXIT", T0, {"position_id": "p", "exit_reason": reason, "net_pnl_inr": pnl, "gross_pnl_inr": pnl})


class ReportTests(TmpCase):
    def fill(self, source, pnls):
        j = J.Journal(self.path, source, "r1")
        j.append("RUN_START", T0, {})
        j.append("DECISION", T0, {"status": "NO_TRADE"})
        j.append("DECISION", T0, {"status": "BLOCKED", "risk": {"reason_codes": ["PER_TRADE_RISK_EXCEEDED"]}})
        j.append("APPROVAL", T0, {"accepted": True})
        j.append("APPROVAL", T0, {"accepted": False})
        for p in pnls:
            exit_record(j, p)

    def test_fixture_report_carries_the_not_evidence_line_and_shows_sample_size(self):
        self.fill("fixture", ["100.50", "-250.25"])
        rep = REP.build_report(self.path)
        self.assertEqual(rep["caveat"], "Simulation on synthetic or model-priced data. This is not evidence of profitability.")
        self.assertEqual((rep["sample_size"], rep["net_pnl_inr"], rep["largest_loss_inr"], rep["wins"], rep["losses"]),
                         (2, "-149.75", "-250.25", 1, 1))
        self.assertEqual(rep["decision_status"], {"BLOCKED": 1, "NO_TRADE": 1})
        self.assertEqual(rep["blocked_reason_counts"], {"PER_TRADE_RISK_EXCEEDED": 1})
        text = REP.render_report(rep)
        self.assertIn("n=2", text)
        self.assertIn("PAPER", text)
        self.assertIn("Not financial advice", text)
        self.assertIn("not evidence of profitability", text)

    def test_golden_report_text(self):
        self.fill("fixture", ["100.50"])
        text = REP.render_report(REP.build_report(self.path)).replace(self.path, "<journal>")
        self.assertEqual(text, "\n".join([
            "PAPER - SIMULATED | PAPER trading only. Not financial advice.",
            "journal: <journal> (6 records, chain OK)",
            "data source: fixture | runs: 1 | decisions: 2",
            "  BLOCKED              n=1",
            "  NO_TRADE             n=1",
            "blocked by (counts): PER_TRADE_RISK_EXCEEDED x1",
            "approvals recorded: 2 (accepted 1)",
            "completed paper trades: n=1",
            "net P&L after simulated costs: 100.50 INR (n=1); largest loss: 0 INR; wins 1, losses 0",
            "exit reasons: PROFIT_TARGET x1",
            "Simulation on synthetic or model-priced data. This is not evidence of profitability."]))

    def test_real_data_report_makes_no_performance_claim_below_the_minimum(self):
        self.fill("real", ["100", "200"])
        rep = REP.build_report(self.path)
        self.assertIsNone(rep["net_pnl_inr"])
        self.assertEqual(rep["sample_size"], 2)
        self.assertIn("no performance claim", rep["caveat"])
        self.assertIn("net P&L: not shown (n=2)", REP.render_report(rep))
        shown = REP.build_report(self.path, min_real_trades=2)
        self.assertEqual(shown["net_pnl_inr"], "300")
        self.assertIn("Sample of 2", shown["caveat"])

    def test_a_mixed_journal_gets_no_report(self):
        os.makedirs(os.path.dirname(self.path))
        a = J.Journal(self.path, "fixture", "r1").append("RUN_START", T0, {})
        with open(self.path, "a", encoding="utf-8") as fh:                      # a hand-built mix
            body = {"seq": 2, "at": T0.isoformat(), "run_id": "r", "kind": "RUN_START", "data_source": "real",
                    "paper": True, "payload": {}, "prev_hash": a["hash"]}
            fh.write(json.dumps(dict(body, hash=J._hash(body))) + "\n")
        rep = REP.build_report(self.path)
        self.assertIn("error", rep)
        self.assertFalse(rep["journal_ok"])
        self.assertIn("mixes", REP.render_report(rep))

    def test_empty_journal(self):
        rep = REP.build_report(self.path)
        self.assertEqual((rep["records"], rep["caveat"]), (0, "The journal is empty."))

    def test_critical_exceptions_are_reported(self):
        j = J.Journal(self.path, "fixture", "r1")
        j.append("CRITICAL", T0, {"text": "x"})
        self.assertIn("CRITICAL EXCEPTIONS: 1", REP.render_report(REP.build_report(self.path)))


class TicketTests(NoNetworkTestCase):
    def setUp(self):
        super(TicketTests, self).setUp()
        cfg = sc.config()._replace(risk_per_trade_inr=Dec("5000"))
        self.cand = TR.cheap_candidate("30.00")
        led = L.DayLedger(self.cand.decision_time.date(), cfg.account_capital_inr)
        self.rd = R.evaluate(self.cand, led, self.cand.decision_time, cfg, TR.ENGINE, sc.specs(), sc.calendar(),
                             tempfile.mkdtemp())
        self.assertEqual(self.rd.outcome, R.APPROVED)
        self.t = A.make_ticket(self.cand, self.rd)

    def test_ticket_content(self):
        self.assertEqual(self.t.fingerprint, R.candidate_fingerprint(self.cand)[:16])
        self.assertEqual(len(self.t.fingerprint), 16)
        for piece in ("PAPER ORDER", "no real order", "Bull Call Debit Spread", self.cand.long_contract.symbol,
                      self.cand.short_contract.symbol, "65 units", "Total defined risk INR 2140.36",
                      "per-trade limit INR 5000.00", self.t.fingerprint):
            self.assertIn(piece, self.t.text)

    def test_only_a_reply_starting_with_yes_counts(self):
        for reply in ("yes", "Yes", "YES.", "yes, go ahead", "yes please", "  yes  "):
            self.assertTrue(A.parse_yes(reply, self.t)[0], reply)
        for reply in ("", "   ", None, "ok", "sure", "go", "do it", "y", "yeah", "no", "approve", "I say yes"):
            self.assertFalse(A.parse_yes(reply, self.t)[0], repr(reply))

    def test_questions_conditions_doubts_and_batches_do_not_count(self):
        for reply in ("yes?", "yes, is that right?", "yes but only if it is cheaper", "yes if the risk is lower",
                      "maybe yes", "yes, probably", "yes unless it moves", "yes, I think so", "yes not sure",
                      "yes to all", "yes both", "yes and every future one", "yes always", "yes everything",
                      "yes wait", "yes, later"):
            ok, why = A.parse_yes(reply, self.t)
            self.assertFalse(ok, reply)
            self.assertTrue(why)

    def test_numbers_in_the_reply_must_be_on_the_ticket(self):
        self.assertTrue(A.parse_yes("yes, 1 lot", self.t)[0])
        self.assertTrue(A.parse_yes("yes for 2140.36", self.t)[0])
        self.assertTrue(A.parse_yes("yes, %s" % self.cand.long_contract.strike, self.t)[0])
        self.assertTrue(A.parse_yes("yes 2,140.36", self.t)[0])
        for reply in ("yes 5 lots", "yes for 3000", "yes 2140.37", "yes 25000"):
            ok, why = A.parse_yes(reply, self.t)
            self.assertFalse(ok, reply)
            self.assertIn("not a figure on this order", why)

    def test_a_fingerprint_in_the_reply_must_be_this_orders(self):
        self.assertTrue(A.parse_yes("yes %s" % self.t.fingerprint, self.t)[0])
        ok, why = A.parse_yes("yes 0123456789abcdef", self.t)
        self.assertFalse(ok)
        self.assertIn("names fingerprint", why)

    def test_a_ticket_for_a_different_candidate_has_a_different_fingerprint(self):
        other = TR.cheap_candidate("30.05", at=self.cand.decision_time + datetime.timedelta(minutes=1))
        cfg = sc.config()._replace(risk_per_trade_inr=Dec("5000"))
        led = L.DayLedger(other.decision_time.date(), cfg.account_capital_inr)
        rd = R.evaluate(other, led, other.decision_time, cfg, TR.ENGINE, sc.specs(), sc.calendar(), tempfile.mkdtemp())
        self.assertNotEqual(A.make_ticket(other, rd).fingerprint, self.t.fingerprint)

    def test_parser_has_no_default_or_automatic_yes(self):
        self.assertFalse(A.parse_yes(None, self.t)[0])
        self.assertEqual(A.YES_WORDS, frozenset(["yes"]))


if __name__ == "__main__":
    unittest.main()
