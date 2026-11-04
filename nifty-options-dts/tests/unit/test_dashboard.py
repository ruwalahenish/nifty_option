"""The local dashboard: pages build from a journal, escape their input, and stay read-only. No socket is opened."""
import os
import tempfile
import unittest

from nifty_core import cli
from tests._base import NoNetworkTestCase
from tools import backtest, dashboard

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Sink(object):
    def write(self, text):
        pass


class DashboardTests(NoNetworkTestCase):
    def setUp(self):
        super(DashboardTests, self).setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = self.tmp.name
        path = os.path.join(self.folder, "bullish.jsonl")
        cli.main(["run-day", "--scenario", "bullish_trend", "--journal", path, "--kill-dir", self.folder], _Sink())

    def test_index_lists_the_journal(self):
        status, text = dashboard.route(self.folder, "/")
        self.assertEqual(status, 200)
        self.assertIn("bullish.jsonl", text)
        self.assertIn("PAPER TRADING ONLY", text)

    def test_journal_page_carries_the_fixture_caveat_and_no_action_controls(self):
        status, text = dashboard.route(self.folder, "/journal?name=bullish.jsonl")
        self.assertEqual(status, 200)
        self.assertIn("not evidence of profitability", text)
        for control in ("<form", "<button", "<input", "<script"):
            self.assertNotIn(control, text)

    def test_unknown_or_path_like_names_are_not_served(self):
        for bad in ("/journal?name=../config/sources.json", "/journal?name=nope.jsonl", "/journal", "/etc/passwd"):
            self.assertEqual(dashboard.route(self.folder, bad)[0], 404, bad)

    def test_input_is_escaped(self):
        self.assertEqual(dashboard.esc("<script>x</script>"), "&lt;script&gt;x&lt;/script&gt;")

    def test_only_get_is_allowed_and_it_binds_to_this_computer(self):
        handler = dashboard.make_handler(self.folder)
        self.assertEqual(dashboard.BIND_HOST, "127.0.0.1")
        self.assertTrue(hasattr(handler, "do_GET"))
        for verb in ("do_POST", "do_PUT", "do_DELETE", "do_PATCH"):
            self.assertIs(getattr(handler, verb), handler._refuse)

    def test_missing_folder_gives_an_empty_index(self):
        status, text = dashboard.route(os.path.join(self.folder, "missing"), "/")
        self.assertEqual(status, 200)
        self.assertIn("No journals", text)


class BacktestTests(NoNetworkTestCase):
    def test_batch_runs_every_fixture_and_enters_nothing_without_answers(self):
        with tempfile.TemporaryDirectory() as d:
            files = backtest.scenario_files(os.path.join(ROOT, "tests", "fixtures", "scenarios"))
            results = backtest.run_backtest(files, None, d, "1", {}, _Sink())
            self.assertEqual(len(results), len(files))
            for r in results:
                if "error" in r:
                    self.fail(r)
                self.assertEqual(r["entries"], 0)           # no automatic approval
            self.assertTrue(os.path.exists(os.path.join(d, "backtest_fixture.jsonl")))


if __name__ == "__main__":
    unittest.main()
