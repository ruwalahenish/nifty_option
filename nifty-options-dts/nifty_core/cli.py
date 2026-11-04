"""Command-line front-end for the paper-trading pipeline. The skills call this; it holds no logic of its own.

    python -m nifty_core.cli regime   --scenario NAME_OR_PATH --at HH:MM
    python -m nifty_core.cli candidate --scenario ... --at HH:MM
    python -m nifty_core.cli risk     --scenario ... --at HH:MM
    python -m nifty_core.cli ticket   --scenario ... --at HH:MM
    python -m nifty_core.cli run-day  --scenario ... [--journal PATH] [--answer FINGERPRINT=REPLY ...]
    python -m nifty_core.cli report   --journal PATH
    python -m nifty_core.cli verify-journal --journal PATH

PAPER ONLY. There is no flag to go live, to skip the kill switch, or to approve automatically: the only way to approve
an order is `--answer FINGERPRINT="yes"` for that exact ticket, recorded word for word. Data comes from a scenario
file (fixtures); Version 1 has no real-data provider. Risk limits come from the config file, read once at the start.
"""
import argparse
import json
import os
import sys

from . import approval as approval_mod
from . import config as config_mod
from . import pipeline
from .data.fixture_provider import FixtureProvider, load_scenario_file
from .errors import NiftyError
from .journal import journal as journal_mod
from .journal import report as report_mod
from .market.ist import parse_ist
from .regime import engine as regime_engine
from .risk import engine as risk_engine
from .risk import ledger as ledger_mod
from .strategy import engine as strategy_engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "scenarios")
BANNER = "PAPER TRADING ONLY | SIMULATED (synthetic fixture data) | Not financial advice. No real order is ever sent."


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SystemExit("error: %s" % message)


def _scenario_path(arg):
    if os.path.exists(arg):
        return arg
    candidate = os.path.join(FIXTURE_DIR, arg if arg.endswith(".json") else arg + ".json")
    if os.path.exists(candidate):
        return candidate
    raise SystemExit("error: no scenario file %r (give a path or a fixture name such as bullish_trend)" % arg)


def _load(args):
    cfg = config_mod.load_config(args.config)
    ctx = pipeline.load_context(cfg)
    scenario = load_scenario_file(_scenario_path(args.scenario), ctx.specs, ctx.calendar, ctx.data_mode)
    return ctx, scenario, FixtureProvider(scenario)


def _at(args, scenario):
    return parse_ist("%s %s" % (scenario.day.isoformat(), args.at))


def _print(out, args, text, data):
    if getattr(args, "json", False):
        out.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    else:
        out.write(BANNER + "\n" + text + "\n")


def cmd_regime(args, out):
    ctx, scenario, provider = _load(args)
    t = _at(args, scenario)
    r = regime_engine.classify_at(provider, ctx.calendar, t, ctx.regime_params, ctx.data_mode)
    lines = ["regime at %s: %s" % (t.strftime("%Y-%m-%d %H:%M IST"), r.label)]
    lines += ["  reason %s: %s" % (x.code, x.text) for x in r.reasons]
    lines += ["  evidence %-16s %-7s %s" % (e.name, e.vote, e.observed) for e in r.evidence]
    if r.evidence_score is not None:
        lines.append("  evidence score %s (a model summary of how the evidence leans; not a probability of profit)"
                     % r.evidence_score)
    _print(out, args, "\n".join(lines), r.to_journal_dict())
    return 0


def cmd_candidate(args, out):
    ctx, scenario, provider = _load(args)
    t = _at(args, scenario)
    d = strategy_engine.decide_at(provider, ctx.calendar, ctx.specs, ctx.cfg, ctx.strategy_params, ctx.regime_params, t,
                                  ctx.data_mode)
    lines = ["strategy at %s: regime %s -> %s" % (t.strftime("%H:%M"), d.regime.label, d.outcome)]
    lines += ["  reason %s: %s" % (r.code, r.text) for r in d.reasons]
    if d.candidate:
        c = d.candidate
        lines.append("  %s buy %s sell %s, net debit %s pts (INR %s), width %s" % (
            c.strategy_type, c.long_contract.symbol, c.short_contract.symbol, c.net_debit_points,
            c.net_debit_inr.quantize(c.net_debit_inr.__class__("0.01")), c.width_points))
        lines.append("  A candidate is a proposal only: it has not been risk-checked or approved.")
    _print(out, args, "\n".join(lines), strategy_engine.decision_to_journal(d))
    return 0


def _risk_view(args):
    ctx, scenario, provider = _load(args)
    t = _at(args, scenario)
    d = strategy_engine.decide_at(provider, ctx.calendar, ctx.specs, ctx.cfg, ctx.strategy_params, ctx.regime_params, t,
                                  ctx.data_mode)
    if d.candidate is None:
        return ctx, d, None, t
    led = ledger_mod.DayLedger(t.date(), ctx.cfg.account_capital_inr)
    rd = risk_engine.evaluate(d.candidate, led, t, ctx.cfg, ctx.cost_engine, ctx.specs, ctx.calendar, args.kill_dir)
    return ctx, d, rd, t


def cmd_risk(args, out):
    ctx, d, rd, t = _risk_view(args)
    if rd is None:
        text = "no candidate to check (strategy: %s)\n%s" % (d.outcome, "\n".join(
            "  reason %s: %s" % (r.code, r.text) for r in d.reasons))
        _print(out, args, text, strategy_engine.decision_to_journal(d))
        return 0
    lines = ["risk at %s: %s (a fresh day ledger is assumed)" % (t.strftime("%H:%M"), rd.outcome)]
    if rd.calculation:
        c = rd.calculation
        lines.append("  total defined risk INR %s = net debit %s + slippage %s + entry costs %s + estimated exit costs %s; "
                     "limit INR %s" % (c["total_defined_risk_inr"], c["net_debit_quoted_inr"], c["entry_slippage_inr"],
                                       c["entry_costs_inr"], c["estimated_exit_costs_inr"],
                                       rd.limits["risk_per_trade_inr"]))
    lines += ["  reason %s: %s" % (r.code, r.text) for r in rd.reasons]
    _print(out, args, "\n".join(lines), risk_engine.decision_to_journal(rd))
    return 0


def cmd_ticket(args, out):
    ctx, d, rd, t = _risk_view(args)
    if rd is None or rd.outcome != risk_engine.APPROVED:
        why = d.reasons if rd is None else rd.reasons
        text = "no ticket: nothing was approved by the strategy and risk engines\n" + "\n".join(
            "  reason %s: %s" % (r.code, r.text) for r in why)
        _print(out, args, text, {"ticket": None, "reasons": [{"code": r.code, "text": r.text} for r in why]})
        return 0
    tk = approval_mod.make_ticket(d.candidate, rd)
    text = (tk.text + "\nTo approve this one order, run `run-day` with:  --answer %s=\"yes\"\n"
            "Your reply is recorded word for word. Only a reply that starts with \"yes\" counts." % tk.fingerprint)
    _print(out, args, text, {"ticket": tk.text, "fingerprint": tk.fingerprint})
    return 0


def _parse_answers(items):
    answers = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit("error: --answer needs FINGERPRINT=REPLY, got %r" % item)
        fp, reply = item.split("=", 1)
        answers[fp.strip()] = reply
    return answers


def cmd_run_day(args, out):
    ctx, scenario, provider = _load(args)
    path = args.journal or os.path.join("trader", "journal", "%s.jsonl" % scenario.name)
    source = journal_mod.data_source_for_label(scenario.data_label)
    run_id = "run-%s-%s" % (scenario.name, args.run_label)
    jr = journal_mod.Journal(path, source, run_id)
    summary = pipeline.run_day(provider, scenario.day, ctx, args.kill_dir, jr, _parse_answers(args.answer))
    lines = ["run of %s (%s) finished. journal: %s" % (scenario.name, scenario.day, summary.journal_path),
             "  decisions: " + (", ".join("%s x%d" % kv for kv in summary.status_counts.items()) or "none"),
             "  entries %d | completed trades %d | critical exceptions %d | net P&L INR %s" % (
                 summary.entries, summary.exits, summary.criticals, summary.net_pnl_inr)]
    for fp in dict.fromkeys(summary.tickets_awaiting):
        lines.append("  TICKET %s is AWAITING your approval. Re-run with --answer %s=\"yes\" to approve exactly it." % (fp, fp))
    lines.append("  " + report_mod.FIXTURE_CAVEAT)
    _print(out, args, "\n".join(lines), {"summary": summary._asdict()})
    return 0


def cmd_report(args, out):
    rep = report_mod.build_report(args.journal)
    _print(out, args, report_mod.render_report(rep), rep)
    return 0 if rep["journal_ok"] and "error" not in rep else 1


def cmd_verify(args, out):
    res = journal_mod.verify(args.journal)
    text = "journal %s: %s (%d records)%s" % (args.journal, "OK" if res.ok else "BROKEN", res.records,
                                              "".join("\n  " + p for p in res.problems))
    _print(out, args, text, {"ok": res.ok, "records": res.records, "problems": res.problems})
    return 0 if res.ok else 1


def build_parser():
    p = _Parser(prog="nifty_core.cli", description="NIFTY options PAPER-trading pipeline. Not financial advice.",
                allow_abbrev=False)
    sub = p.add_subparsers(dest="command")
    sub.required = True

    def common(sp, scenario=True, at=True):
        sp.add_argument("--json", action="store_true", help="print machine-readable JSON")
        if scenario:
            sp.add_argument("--scenario", required=True, help="fixture name or path to a scenario JSON file")
            sp.add_argument("--config", default=None, help="config JSON (default: config/config.example.json)")
        if at:
            sp.add_argument("--at", required=True, help="decision time HH:MM IST on the scenario's day")
        return sp
    for name, fn in (("regime", cmd_regime), ("candidate", cmd_candidate)):
        common(sub.add_parser(name, allow_abbrev=False)).set_defaults(fn=fn)
    for name, fn in (("risk", cmd_risk), ("ticket", cmd_ticket)):
        sp = common(sub.add_parser(name, allow_abbrev=False))
        sp.add_argument("--kill-dir", default="trader", help="folder where a KILL file stops entries")
        sp.set_defaults(fn=fn)
    sp = common(sub.add_parser("run-day", allow_abbrev=False), at=False)
    sp.add_argument("--journal", default=None, help="journal file (default trader/journal/<scenario>.jsonl)")
    sp.add_argument("--answer", action="append", default=[], metavar="FINGERPRINT=REPLY",
                    help="your reply for one exact ticket, recorded word for word")
    sp.add_argument("--kill-dir", default="trader", help="folder where a KILL file stops entries")
    sp.add_argument("--run-label", default="1", help="label that distinguishes repeated runs in one journal")
    sp.set_defaults(fn=cmd_run_day)
    sp = common(sub.add_parser("report", allow_abbrev=False), scenario=False, at=False)
    sp.add_argument("--journal", required=True)
    sp.set_defaults(fn=cmd_report)
    sp = common(sub.add_parser("verify-journal", allow_abbrev=False), scenario=False, at=False)
    sp.add_argument("--journal", required=True)
    sp.set_defaults(fn=cmd_verify)
    return p


def main(argv=None, out=None):
    out = out or sys.stdout
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args, out)
    except NiftyError as e:
        out.write("%s\nrefused: %s\n" % (BANNER, e))
        return 2


if __name__ == "__main__":
    sys.exit(main())
