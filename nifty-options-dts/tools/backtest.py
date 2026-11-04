"""Replay many recorded days through the paper pipeline, one journal per data source.

    python tools/backtest.py --scenarios tests/fixtures/scenarios
    python tools/backtest.py --scenarios data --config my_real_config.json --answers answers.json --run-label 2

PAPER ONLY. This is a batch version of `run-day`; it adds no logic and loosens no rule. In particular there is NO
automatic approval: a day only enters a paper trade if you supply a reply for that exact ticket.

    Pass 1: run without --answers. Every approved candidate prints its ticket fingerprint and is left AWAITING.
    Pass 2: read the tickets (`nifty_core.cli ticket` or the dashboard), write the fingerprints you agree with into an
            answers file {"FINGERPRINT": "yes"} and re-run with --answers FILE --run-label 2.

The answers file is your reply, recorded word for word in the journal like any other reply. Risk limits, the kill
switch, the paper-only guard and the real-data rules (DATA_MODE_MISMATCH, DAY_ALREADY_TRADED...) apply unchanged.
A backtest on a handful of days, or on synthetic fixtures, is not evidence of profitability.
"""
import argparse
import json
import os
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from nifty_core import config as config_mod          # noqa: E402
from nifty_core import pipeline                      # noqa: E402
from nifty_core.data.fixture_provider import FixtureProvider, load_scenario_file   # noqa: E402
from nifty_core.errors import NiftyError             # noqa: E402
from nifty_core.journal import journal as journal_mod    # noqa: E402
from nifty_core.journal import report as report_mod      # noqa: E402

DEFAULT_JOURNAL_DIR = os.path.join(PROJECT, "trader", "journal")
KILL_DIR = os.path.join(PROJECT, "trader")


def scenario_files(path):
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path):
        raise SystemExit("error: %r is neither a scenario file nor a folder" % path)
    return sorted(os.path.join(path, n) for n in os.listdir(path) if n.endswith(".json"))


def load_answers(path):
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise SystemExit('error: the answers file must be a JSON object of {"FINGERPRINT": "reply"}')
    return data


def run_backtest(files, config_path, journal_dir, run_label, answers, out):
    """Run every file. Returns a list of per-day result dicts (a refused day has an 'error' instead of numbers)."""
    cfg = config_mod.load_config(config_path)
    ctx = pipeline.load_context(cfg, cfg.data_mode)
    results = []
    for path in files:
        name = os.path.basename(path)
        try:
            scenario = load_scenario_file(path, ctx.specs, ctx.calendar, ctx.data_mode)
            if (scenario.data_label == "FIXTURE_SYNTHETIC") != (cfg.data_mode == "fixture"):
                raise NiftyError("DATA_MODE_MISMATCH: %s is labelled %s but the config data_mode is %r" % (
                    name, scenario.data_label, cfg.data_mode))
            source = journal_mod.data_source_for_label(scenario.data_label)
            jpath = os.path.join(journal_dir, "backtest_%s.jsonl" % source)
            jr = journal_mod.Journal(jpath, source, "bt-%s-%s" % (scenario.name, run_label))
            s = pipeline.run_day(FixtureProvider(scenario), scenario.day, ctx, KILL_DIR, jr, answers)
            results.append({"scenario": scenario.name, "day": s.day.isoformat(), "journal": jpath,
                            "decisions": s.status_counts, "entries": s.entries, "completed_trades": s.exits,
                            "critical_exceptions": s.criticals, "net_pnl_inr": str(s.net_pnl_inr),
                            "tickets_awaiting": list(dict.fromkeys(s.tickets_awaiting))})
        except (NiftyError, ValueError) as e:
            results.append({"scenario": name, "error": str(e)})
        r = results[-1]
        if "error" in r:
            out.write("  %-28s REFUSED: %s\n" % (r["scenario"], r["error"]))
        else:
            out.write("  %-28s %s entries %d trades %d net INR %s | %s\n" % (
                r["scenario"], r["day"], r["entries"], r["completed_trades"], r["net_pnl_inr"],
                ", ".join("%s x%d" % kv for kv in r["decisions"].items()) or "no decisions"))
    return results


def main(argv=None, out=None):
    out = out or sys.stdout
    p = argparse.ArgumentParser(description="Batch paper replay of recorded days. Not financial advice.",
                                allow_abbrev=False)
    p.add_argument("--scenarios", required=True, help="a scenario JSON file or a folder of them")
    p.add_argument("--config", default=None, help="config JSON (default: config/config.example.json)")
    p.add_argument("--journal-dir", default=DEFAULT_JOURNAL_DIR)
    p.add_argument("--answers", default=None, help='JSON file {"FINGERPRINT": "yes"} that you wrote after reading the tickets')
    p.add_argument("--run-label", default="1", help="use a new label for each pass over the same days")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    files = scenario_files(args.scenarios)
    if not files:
        raise SystemExit("error: no .json scenario files found in %s" % args.scenarios)
    out.write("PAPER TRADING ONLY | SIMULATED fills | Not financial advice. No real order is ever sent.\n")
    results = run_backtest(files, args.config, args.journal_dir, args.run_label, load_answers(args.answers), out)
    good = [r for r in results if "error" not in r]
    tickets = [fp for r in good for fp in r["tickets_awaiting"]]
    total = sum((Decimal(r["net_pnl_inr"]) for r in good), Decimal(0))
    trades = sum(r["completed_trades"] for r in good)
    out.write("days run %d of %d | completed paper trades %d | net P&L after simulated costs INR %s\n" % (
        len(good), len(results), trades, total))
    if tickets:
        out.write("%d ticket(s) AWAITING your reply. Read them, then re-run with --answers FILE --run-label <new>:\n" %
                  len(tickets))
        for fp in tickets:
            out.write("  %s\n" % fp)
    out.write(report_mod.FIXTURE_CAVEAT + " With real data, no performance claim is made below %d completed trades.\n" %
              report_mod.MIN_REAL_TRADES)
    if args.json:
        out.write(json.dumps(results, indent=2, default=str) + "\n")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
