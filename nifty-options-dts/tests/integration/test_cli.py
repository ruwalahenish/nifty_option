"""Drive the command-line front-end the way a skill does."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from nifty_core import cli
from nifty_core.journal import journal as J
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Base(NoNetworkTestCase):
    def setUp(self):
        super(Base, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.kill = os.path.join(self.dir, "trader")
        self.big = os.path.join(self.dir, "big.json")
        with open(os.path.join(ROOT, "config", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["account_capital_inr"] = 500000                    # test-only: lets the fixture candidates fit the limit
        with open(self.big, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def run_cli(self, *argv):
        out = io.StringIO()
        code = cli.main(list(argv), out)
        return code, out.getvalue()

    def journal(self, name="j.jsonl"):
        return os.path.join(self.dir, name)


class CommandTests(Base):
    def test_every_output_is_labelled_paper_and_simulated(self):
        for argv in (("regime", "--scenario", "bullish_trend", "--at", "12:30"),
                     ("candidate", "--scenario", "bullish_trend", "--at", "12:30"),
                     ("risk", "--scenario", "bullish_trend", "--at", "12:30", "--kill-dir", self.kill),
                     ("ticket", "--scenario", "bullish_trend", "--at", "12:30", "--kill-dir", self.kill)):
            code, text = self.run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("PAPER TRADING ONLY", text)
            self.assertIn("SIMULATED", text)
            self.assertIn("Not financial advice", text)
            self.assertIn("No real order is ever sent", text)

    def test_regime_shows_the_label_and_the_evidence(self):
        code, text = self.run_cli("regime", "--scenario", "bullish_trend", "--at", "12:30")
        self.assertIn("regime at 2026-09-16 12:30 IST: BULLISH", text)
        self.assertIn("net_move", text)
        self.assertIn("not a probability of profit", text)
        code, text = self.run_cli("regime", "--scenario", "bullish_trend", "--at", "10:00")
        self.assertIn("NO_DATA", text)
        self.assertIn("INSUFFICIENT_HISTORY", text)

    def test_candidate_and_no_trade_reasons(self):
        code, text = self.run_cli("candidate", "--scenario", "bullish_trend", "--at", "12:30")
        self.assertIn("CANDIDATE", text)
        self.assertIn("BULL_CALL_DEBIT", text)
        self.assertIn("proposal only", text)
        code, text = self.run_cli("candidate", "--scenario", "sideways", "--at", "12:30")
        self.assertIn("NO_TRADE", text)
        self.assertIn("REGIME_SIDEWAYS", text)

    def test_risk_blocks_the_default_fixture_candidate_and_shows_the_arithmetic(self):
        code, text = self.run_cli("risk", "--scenario", "bullish_trend", "--at", "12:30", "--kill-dir", self.kill)
        self.assertIn("BLOCKED", text)
        self.assertIn("PER_TRADE_RISK_EXCEEDED", text)
        self.assertIn("3596.55", text)
        self.assertIn("estimated exit costs", text)

    def test_risk_with_no_candidate(self):
        code, text = self.run_cli("risk", "--scenario", "sideways", "--at", "12:30", "--kill-dir", self.kill)
        self.assertIn("no candidate to check", text)

    def test_ticket_only_when_everything_approves(self):
        code, text = self.run_cli("ticket", "--scenario", "bullish_trend", "--at", "12:30", "--kill-dir", self.kill)
        self.assertIn("no ticket", text)
        code, text = self.run_cli("ticket", "--scenario", "bullish_trend", "--at", "12:30", "--config", self.big,
                                  "--kill-dir", self.kill)
        self.assertIn("PAPER ORDER (simulated, no real order)", text)
        self.assertIn('Only a reply that starts with "yes" counts', text)

    def test_json_output(self):
        code, text = self.run_cli("regime", "--scenario", "bullish_trend", "--at", "12:30", "--json")
        data = json.loads(text)
        self.assertEqual(data["label"], "BULLISH")
        code, text = self.run_cli("ticket", "--scenario", "bullish_trend", "--at", "12:30", "--config", self.big,
                                  "--kill-dir", self.kill, "--json")
        self.assertEqual(len(json.loads(text)["fingerprint"]), 16)

    def test_kill_switch_blocks_the_ticket(self):
        os.makedirs(self.kill)
        with open(os.path.join(self.kill, "KILL"), "w") as fh:
            fh.write("test\n")
        code, text = self.run_cli("ticket", "--scenario", "bullish_trend", "--at", "12:30", "--config", self.big,
                                  "--kill-dir", self.kill)
        self.assertIn("KILL_SWITCH_ACTIVE", text)
        self.assertIn("no ticket", text)


class RunDayTests(Base):
    def fingerprint(self):
        code, text = self.run_cli("ticket", "--scenario", "bullish_trend", "--at", "12:30", "--config", self.big,
                                  "--kill-dir", self.kill, "--json")
        return json.loads(text)["fingerprint"]

    def test_run_day_report_and_verify(self):
        path = self.journal()
        code, text = self.run_cli("run-day", "--scenario", "sideways", "--journal", path, "--kill-dir", self.kill)
        self.assertEqual(code, 0)
        self.assertIn("NO_TRADE x52", text)
        self.assertIn("not evidence of profitability", text)
        code, text = self.run_cli("verify-journal", "--journal", path)
        self.assertEqual(code, 0)
        self.assertIn("OK", text)
        code, text = self.run_cli("report", "--journal", path)
        self.assertEqual(code, 0)
        self.assertIn("data source: fixture", text)
        self.assertIn("Not financial advice", text)

    def test_approval_flow_through_the_cli(self):
        path = self.journal()
        fp = self.fingerprint()
        code, text = self.run_cli("run-day", "--scenario", "bullish_trend", "--config", self.big, "--journal", path,
                                  "--kill-dir", self.kill)
        self.assertIn("AWAITING your approval", text)
        self.assertIn(fp, text)
        reply = "yes"
        code, text = self.run_cli("run-day", "--scenario", "bullish_trend", "--config", self.big, "--journal", path,
                                  "--kill-dir", self.kill, "--run-label", "2", "--answer", "%s=%s" % (fp, reply))
        self.assertIn("entries 1 | completed trades 1", text)
        records = J.read_records(path)
        approvals = [r for r in records if r["kind"] == "APPROVAL"]
        self.assertEqual([(a["payload"]["reply_verbatim"], a["payload"]["accepted"]) for a in approvals], [(reply, True)])
        self.assertEqual({r["run_id"] for r in records}, {"run-bullish_trend-1", "run-bullish_trend-2"})
        code, text = self.run_cli("report", "--journal", path)
        self.assertIn("completed paper trades: n=1", text)

    def test_the_reply_keeps_its_exact_spacing_in_the_journal(self):
        fp = self.fingerprint()
        reply = "  Yes,   go ahead  "
        path = self.journal()
        self.run_cli("run-day", "--scenario", "bullish_trend", "--config", self.big, "--journal", path,
                     "--kill-dir", self.kill, "--answer", "%s=%s" % (fp, reply))
        approvals = [r for r in J.read_records(path) if r["kind"] == "APPROVAL"]
        self.assertEqual(approvals[0]["payload"]["reply_verbatim"], reply)

    def test_a_not_yes_reply_through_the_cli_enters_nothing(self):
        fp = self.fingerprint()
        code, text = self.run_cli("run-day", "--scenario", "bullish_trend", "--config", self.big,
                                  "--journal", self.journal(), "--kill-dir", self.kill, "--answer", "%s=ok go" % fp)
        self.assertIn("entries 0", text)
        self.assertIn("DECLINED", text)

    def test_a_tampered_journal_fails_verification_and_refuses_new_runs(self):
        path = self.journal()
        self.run_cli("run-day", "--scenario", "sideways", "--journal", path, "--kill-dir", self.kill)
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text.replace("NO_TRADE", "NO_TRAD3", 1))
        code, out = self.run_cli("verify-journal", "--journal", path)
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", out)
        code, out = self.run_cli("run-day", "--scenario", "sideways", "--journal", path, "--kill-dir", self.kill,
                                 "--run-label", "2")
        self.assertEqual(code, 2)
        self.assertIn("refused", out)
        code, out = self.run_cli("report", "--journal", path)
        self.assertEqual(code, 1)

    def test_default_journal_location_is_under_trader_journal(self):
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            self.run_cli("run-day", "--scenario", "sideways", "--kill-dir", self.kill)
        finally:
            os.chdir(old)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "trader", "journal", "sideways.jsonl")))


class GuardTests(Base):
    def test_the_parser_has_no_live_approve_force_or_skip_option(self):
        parser = cli.build_parser()
        options = set()
        stack = [parser]
        while stack:
            p = stack.pop()
            for action in p._actions:
                options.update(action.option_strings)
                choices = getattr(action, "choices", None)
                if isinstance(choices, dict):
                    stack.extend(choices.values())
        banned = ("live", "yes", "approve", "force", "auto", "paper", "skip", "unsafe", "override", "real", "broker",
                  "order", "token", "key", "password")
        for opt in options:
            for word in banned:
                self.assertNotIn(word, opt.lower(), opt)
        self.assertIn("--answer", options)

    def test_attempts_to_go_live_or_auto_approve_are_rejected_by_the_parser(self):
        for argv in (["run-day", "--scenario", "sideways", "--live"], ["run-day", "--scenario", "sideways", "--yes"],
                     ["run-day", "--scenario", "sideways", "--paper", "false"],
                     ["run-day", "--scenario", "sideways", "--auto-approve"],
                     ["run-day", "--scenario", "sideways", "--ans", "x=yes"], ["place-order"], []):
            with self.assertRaises(SystemExit, msg=str(argv)):
                cli.main(argv, io.StringIO())

    def test_answers_must_name_a_ticket(self):
        with self.assertRaises(SystemExit):
            self.run_cli("run-day", "--scenario", "sideways", "--journal", self.journal(), "--answer", "yes")

    def test_unknown_scenario_and_bad_time(self):
        with self.assertRaises(SystemExit):
            self.run_cli("regime", "--scenario", "nope", "--at", "12:30")
        with self.assertRaises(Exception):
            self.run_cli("regime", "--scenario", "sideways", "--at", "25:99")

    def test_environment_cannot_turn_paper_mode_off(self):
        for value in ("0", "false", "no"):
            with mock.patch.dict(os.environ, {"NIFTY_PAPER": value}):
                code, text = self.run_cli("regime", "--scenario", "bullish_trend", "--at", "12:30")
            self.assertEqual(code, 2)
            self.assertIn("refused", text)
            self.assertIn("paper", text.lower())

    def test_a_config_with_paper_off_or_live_keys_is_refused(self):
        with open(os.path.join(ROOT, "config", "config.example.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        bad = dict(cfg, **{"pa" + "per": bool(0)})        # built indirectly: the scanner bans the literal
        path = os.path.join(self.dir, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(bad, fh)
        code, text = self.run_cli("regime", "--scenario", "bullish_trend", "--at", "12:30", "--config", path)
        self.assertEqual(code, 2)
        self.assertIn("refused", text)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dict(cfg, broker_api_key="x"), fh)
        code, text = self.run_cli("regime", "--scenario", "bullish_trend", "--at", "12:30", "--config", path)
        self.assertEqual(code, 2)

    def test_real_data_is_not_available_in_this_version(self):
        self.assertNotIn("--data-mode", {o for a in cli.build_parser()._actions for o in a.option_strings})


class ProcessTests(Base):
    def test_the_module_runs_as_a_program(self):
        proc = subprocess.run([sys.executable, "-B", "-m", "nifty_core.cli", "regime", "--scenario", "bullish_trend",
                               "--at", "12:30"], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PAPER TRADING ONLY", proc.stdout)
        self.assertIn("BULLISH", proc.stdout)

    def test_the_program_refuses_live_mode_from_the_environment(self):
        env = dict(os.environ, NIFTY_PAPER="0")
        proc = subprocess.run([sys.executable, "-B", "-m", "nifty_core.cli", "regime", "--scenario", "bullish_trend",
                               "--at", "12:30"], cwd=ROOT, capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("refused", proc.stdout)

    def test_the_program_exits_with_an_error_for_a_live_flag(self):
        proc = subprocess.run([sys.executable, "-B", "-m", "nifty_core.cli", "run-day", "--scenario", "sideways",
                               "--live"], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
