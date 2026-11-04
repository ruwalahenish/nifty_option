"""P12: recorded real data (exported files) through the paper pipeline.

No real market data is stored in this repo. These tests re-date and relabel a synthetic fixture so the plumbing for the
RECORDED_REAL label can be exercised. They prove the rules about labels, modes, journals and the daily limits; they say
nothing about real markets.
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from nifty_core import cli
from nifty_core.journal import journal as J
from tests._base import NoNetworkTestCase

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "scenarios", "bullish_trend.json")
NEW_DAY = "2026-11-05"                                   # a trading day on or after the contract-value verification date
NEW_EXPIRIES = ["2026-11-09", "2026-11-17"]


def redated_fixture(label="RECORDED_REAL", name="recorded_test"):
    with open(FIXTURE, encoding="utf-8") as fh:
        doc = json.load(fh)
    old = sorted({q["expiry"] for ch in doc["chains"] for q in ch["quotes"]})
    text = json.dumps(doc).replace(doc["date"], NEW_DAY)
    for a, b in zip(old, NEW_EXPIRIES):
        text = text.replace(a, b)
    doc = json.loads(text)
    doc["data_label"], doc["scenario"] = label, name
    return doc


class RealBase(NoNetworkTestCase):
    def setUp(self):
        super(RealBase, self).setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.dir, ignore_errors=True))
        self.kill = os.path.join(self.dir, "trader")
        with open(os.path.join(ROOT, "config", "config.example.json"), encoding="utf-8") as fh:
            base = json.load(fh)
        self.fixture_cfg = self.write("fixture_cfg.json", dict(base, account_capital_inr=500000))
        self.real_cfg = self.write("real_cfg.json", dict(base, account_capital_inr=500000, data_mode="real_readonly"))
        self.real_default_cfg = self.write("real_default_cfg.json", dict(base, data_mode="real_readonly"))
        self.real_file = self.write("real.json", redated_fixture())
        self.synthetic_redated = self.write("synthetic.json", redated_fixture("FIXTURE_SYNTHETIC", "synthetic_redated"))

    def write(self, name, obj):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        return path

    def run_cli(self, *argv):
        out = io.StringIO()
        code = cli.main(list(argv), out)
        return code, out.getvalue()

    def run_day(self, journal, cfg=None, scenario=None, *extra):
        return self.run_cli("run-day", "--scenario", scenario or self.real_file, "--config", cfg or self.real_cfg,
                            "--journal", journal, "--kill-dir", self.kill, *extra)

    def records(self, path):
        return J.read_records(path)

    def awaiting_fingerprint(self, text):
        for line in text.splitlines():
            if "TICKET" in line and "AWAITING" in line:
                return line.split()[1]
        self.fail("no ticket in output:\n" + text)


class ModeAndLabelTests(RealBase):
    def test_real_label_with_fixture_mode_is_refused(self):
        code, text = self.run_cli("check-data", "--scenario", self.real_file, "--config", self.fixture_cfg)
        self.assertEqual(code, 2)
        self.assertIn("DATA_MODE_MISMATCH", text)

    def test_synthetic_label_with_real_mode_is_refused(self):
        code, text = self.run_cli("check-data", "--scenario", self.synthetic_redated, "--config", self.real_cfg)
        self.assertEqual(code, 2)
        self.assertIn("DATA_MODE_MISMATCH", text)

    def test_run_day_applies_the_same_check(self):
        for scenario, cfg in ((self.real_file, self.fixture_cfg), (self.synthetic_redated, self.real_cfg)):
            path = os.path.join(self.dir, "m.jsonl")
            code, text = self.run_day(path, cfg, scenario)
            self.assertEqual(code, 2)
            self.assertFalse(os.path.exists(path))              # nothing was written before the refusal

    def test_matching_label_and_mode_pass(self):
        self.assertEqual(self.run_cli("check-data", "--scenario", self.real_file, "--config", self.real_cfg)[0], 0)
        self.assertEqual(self.run_cli("check-data", "--scenario", self.synthetic_redated, "--config", self.fixture_cfg)[0], 0)

    def test_real_mode_refuses_days_before_the_contract_values_were_verified(self):
        doc = redated_fixture()
        text = json.dumps(doc).replace(NEW_DAY, "2026-09-16").replace(NEW_EXPIRIES[0], "2026-09-22").replace(
            NEW_EXPIRIES[1], "2026-09-29")
        old = self.write("old.json", json.loads(text))
        code, out = self.run_cli("check-data", "--scenario", old, "--config", self.real_cfg)
        self.assertEqual(code, 2)
        self.assertIn("no version covers", out)

    def test_check_data_describes_the_file_and_labels_it(self):
        code, text = self.run_cli("check-data", "--scenario", self.real_file, "--config", self.real_cfg)
        self.assertEqual(code, 0)
        for piece in ("RECORDED_REAL", "real_readonly", "recorded real market data", "77", "no order is ever sent"):
            self.assertIn(piece.lower(), text.lower(), piece)
        self.assertNotIn("synthetic fixture data", text)

    def test_a_fixture_file_is_still_labelled_as_synthetic(self):
        code, text = self.run_cli("check-data", "--scenario", "bullish_trend")
        self.assertEqual(code, 0)
        self.assertIn("synthetic", text.lower())
        self.assertIn("not evidence of profitability", text.lower())


class LabelReachesTheEnginesTests(RealBase):
    """In real mode an engine that is not told the data label treats the data as unverified (NO_DATA). Each place that
    classifies the regime must therefore pass the label on."""

    def test_the_regime_command_classifies_real_data(self):
        code, text = self.run_cli("regime", "--scenario", self.real_file, "--config", self.real_cfg, "--at", "12:30")
        self.assertEqual(code, 0)
        self.assertIn("BULLISH", text)
        self.assertNotIn("UNVERIFIED_SOURCE", text)

    def test_the_position_manager_classifies_real_data_when_managing(self):
        from datetime import datetime
        from nifty_core import config as config_mod
        from nifty_core import pipeline
        from nifty_core.data.fixture_provider import FixtureProvider, load_scenario_file
        from nifty_core.market.ist import parse_ist
        from nifty_core.positions import manager as manager_mod
        cfg = config_mod.load_config(self.real_cfg)
        ctx = pipeline.load_context(cfg, cfg.data_mode)
        scenario = load_scenario_file(self.real_file, ctx.specs, ctx.calendar, ctx.data_mode)
        provider = FixtureProvider(scenario)
        pm = manager_mod.PositionManager(cfg, ctx.position_params, None, None, provider, ctx.calendar, ctx.regime_params,
                                         ctx.cost_engine, data_mode=ctx.data_mode, data_label=pipeline._label(provider))
        label = pm._regime(parse_ist("%s 12:30" % scenario.day.isoformat())).label
        self.assertEqual(label, "BULLISH")
        blind = manager_mod.PositionManager(cfg, ctx.position_params, None, None, provider, ctx.calendar, ctx.regime_params,
                                            ctx.cost_engine, data_mode=ctx.data_mode)
        self.assertEqual(blind._regime(parse_ist("%s 12:30" % scenario.day.isoformat())).label, "NO_DATA")

    def test_the_pipeline_hands_the_provider_label_to_the_position_manager(self):
        from nifty_core import pipeline
        from nifty_core.data.fixture_provider import FixtureProvider, load_scenario_file
        from nifty_core import config as config_mod
        cfg = config_mod.load_config(self.real_cfg)
        ctx = pipeline.load_context(cfg, cfg.data_mode)
        provider = FixtureProvider(load_scenario_file(self.real_file, ctx.specs, ctx.calendar, ctx.data_mode))
        self.assertEqual(pipeline._label(provider), "RECORDED_REAL")
        self.assertIsNone(pipeline._label(object()))
        from unittest import mock
        from nifty_core.positions import manager as manager_mod
        seen = []
        real_cls = manager_mod.PositionManager

        def spy(*a, **kw):
            seen.append(kw.get("data_label"))
            return real_cls(*a, **kw)
        with mock.patch.object(manager_mod, "PositionManager", spy):
            self.run_day(os.path.join(self.dir, "spy.jsonl"))
        self.assertEqual(seen, ["RECORDED_REAL"])


class RealRunTests(RealBase):
    def test_a_real_run_lands_in_a_real_journal_with_no_performance_claim(self):
        path = os.path.join(self.dir, "real.jsonl")
        code, text = self.run_day(path, self.real_default_cfg)
        self.assertEqual(code, 0)
        self.assertEqual({r["data_source"] for r in self.records(path)}, {"real"})
        self.assertNotIn("not evidence of profitability", text.lower())
        self.assertIn("recorded real", text.lower())
        code, rep = self.run_cli("report", "--journal", path)
        self.assertEqual(code, 0)
        self.assertIn("Fewer than 30 completed real paper trades", rep)
        self.assertIn("net P&L: not shown", rep)
        self.assertNotIn("synthetic", rep.lower())

    def test_real_data_cannot_be_appended_to_a_fixture_journal(self):
        path = os.path.join(self.dir, "mixed.jsonl")
        self.assertEqual(self.run_cli("run-day", "--scenario", "sideways", "--journal", path, "--kill-dir", self.kill)[0], 0)
        code, text = self.run_day(path)
        self.assertEqual(code, 2)
        self.assertIn("MIXED_DATA_SOURCES", text)

    def test_a_ticket_can_be_answered_on_a_second_run_of_the_same_real_day(self):
        path = os.path.join(self.dir, "answer.jsonl")
        code, text = self.run_day(path)
        fp = self.awaiting_fingerprint(text)
        code, text = self.run_day(path, None, None, "--run-label", "2", "--answer", "%s=yes" % fp)
        self.assertEqual(code, 0)
        self.assertIn("entries 1", text)

    def test_a_real_day_that_already_traded_cannot_be_run_again(self):
        path = os.path.join(self.dir, "twice.jsonl")
        fp = self.awaiting_fingerprint(self.run_day(path)[1])
        self.assertEqual(self.run_day(path, None, None, "--run-label", "2", "--answer", "%s=yes" % fp)[0], 0)
        before = len(self.records(path))
        code, text = self.run_day(path, None, None, "--run-label", "3", "--answer", "%s=yes" % fp)
        self.assertEqual(code, 2)
        self.assertIn("DAY_ALREADY_TRADED", text)
        self.assertEqual(len(self.records(path)), before)       # the refused run wrote nothing

    def test_the_same_rerun_of_a_fixture_day_is_still_allowed(self):
        path = os.path.join(self.dir, "fx.jsonl")
        args = ("--scenario", "bullish_trend", "--config", self.fixture_cfg, "--journal", path, "--kill-dir", self.kill)
        fp = self.awaiting_fingerprint(self.run_cli("run-day", *args)[1])
        self.assertEqual(self.run_cli("run-day", *args, "--run-label", "2", "--answer", "%s=yes" % fp)[0], 0)
        self.assertEqual(self.run_cli("run-day", *args, "--run-label", "3", "--answer", "%s=yes" % fp)[0], 0)

    def test_a_different_real_day_can_be_added_to_the_same_journal(self):
        path = os.path.join(self.dir, "days.jsonl")
        fp = self.awaiting_fingerprint(self.run_day(path)[1])
        self.assertEqual(self.run_day(path, None, None, "--run-label", "2", "--answer", "%s=yes" % fp)[0], 0)
        doc = redated_fixture()
        text = json.dumps(doc).replace(NEW_DAY, "2026-11-06").replace(NEW_EXPIRIES[0], "2026-11-09")
        other = self.write("next_day.json", json.loads(text))
        self.assertEqual(self.run_day(path, None, other, "--run-label", "3")[0], 0)

    def test_the_kill_switch_stops_entries_on_real_data_too(self):
        os.makedirs(self.kill)
        with open(os.path.join(self.kill, "KILL"), "w") as fh:
            fh.write("test\n")
        path = os.path.join(self.dir, "killed.jsonl")
        code, text = self.run_day(path)
        self.assertEqual(code, 0)
        self.assertNotIn("TICKET", text)
        self.assertNotIn("ENTERED", json.dumps(self.records(path)))

    def test_a_script_cannot_loosen_the_paper_rule_with_real_data(self):
        with open(self.real_cfg, encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg[chr(112) + "aper"] = bool(0)
        bad = self.write("notpaper.json", cfg)
        code, text = self.run_cli("check-data", "--scenario", self.real_file, "--config", bad)
        self.assertEqual(code, 2)
        self.assertIn("refused", text)


class CsvConverterTests(RealBase):
    def write_csvs(self):
        doc = redated_fixture()
        cpath, qpath = os.path.join(self.dir, "candles.csv"), os.path.join(self.dir, "chain.csv")
        with open(cpath, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["start", "open", "high", "low", "close", "volume"])
            for c in doc["candles"]:
                w.writerow([c["start"], c["open"], c["high"], c["low"], c["close"], "" if c["volume"] is None else c["volume"]])
        with open(qpath, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow("snapshot_time,spot,expiry,strike,type,bid,ask,ltp,bid_qty,ask_qty,volume,open_interest,timestamp".split(","))
            for ch in doc["chains"]:
                for q in ch["quotes"]:
                    w.writerow([ch["snapshot_time"], ch["spot"], q["expiry"], q["strike"], q["type"], q["bid"], q["ask"],
                                q["ltp"], q["bid_qty"], q["ask_qty"], q["volume"], q["open_interest"], q["timestamp"]])
        return doc, cpath, qpath

    def convert(self, cpath, qpath, out, *extra):
        return subprocess.run([sys.executable, "-B", os.path.join(ROOT, "tools", "csv_to_scenario.py"), "--date", NEW_DAY,
                               "--name", "from_csv", "--candles", cpath, "--chain", qpath, "--out", out] + list(extra),
                              capture_output=True, text=True, cwd=ROOT)

    def test_csv_round_trip_gives_the_same_decisions(self):
        doc, cpath, qpath = self.write_csvs()
        out = os.path.join(self.dir, "from_csv.json")
        r = self.convert(cpath, qpath, out)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out, encoding="utf-8") as fh:
            converted = json.load(fh)
        self.assertEqual(converted["data_label"], "RECORDED_REAL")
        self.assertEqual(converted["chains"], doc["chains"])        # numbers kept as the exported text, nothing rounded
        self.assertEqual(len(converted["candles"]), len(doc["candles"]))
        self.assertEqual(self.run_cli("check-data", "--scenario", out, "--config", self.real_cfg)[0], 0)
        a, b = os.path.join(self.dir, "a.jsonl"), os.path.join(self.dir, "b.jsonl")
        self.run_day(a)
        self.run_day(b, None, out)
        strip = lambda p: [(r["kind"], r["payload"].get("status")) for r in self.records(p)]
        self.assertEqual(strip(a), strip(b))

    def test_a_missing_column_is_refused(self):
        _, cpath, qpath = self.write_csvs()
        with open(qpath, encoding="utf-8") as fh:
            lines = fh.read().replace("bid_qty,", "", 1)
        with open(qpath, "w", encoding="utf-8") as fh:
            fh.write(lines)
        r = self.convert(cpath, qpath, os.path.join(self.dir, "x.json"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("lacks column", r.stderr)

    def test_a_bad_number_is_refused_not_guessed(self):
        _, cpath, qpath = self.write_csvs()
        with open(qpath, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        parts = lines[1].split(",")
        parts[3] = "24x50"                                              # strike
        lines[1] = ",".join(parts)
        with open(qpath, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        r = self.convert(cpath, qpath, os.path.join(self.dir, "x.json"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("whole number", r.stderr)

    def test_a_lot_size_column_in_the_data_is_still_refused_by_the_loader(self):
        doc = redated_fixture()
        doc["chains"][0]["quotes"][0]["lot_size"] = 75
        path = self.write("lot.json", doc)
        code, text = self.run_cli("check-data", "--scenario", path, "--config", self.real_cfg)
        self.assertEqual(code, 2)
        self.assertIn("LOT_SIZE_IN_DATA", text)

    def test_the_converter_does_not_import_networking(self):
        with open(os.path.join(ROOT, "tools", "csv_to_scenario.py"), encoding="utf-8") as fh:
            src = fh.read()
        for word in ("socket", "urllib", "http", "requests", "subprocess"):
            self.assertNotIn("import " + word, src)


if __name__ == "__main__":
    unittest.main()
