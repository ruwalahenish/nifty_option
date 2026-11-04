"""Reports built from a journal. Fixture and real data are never mixed; every statistic shows its sample size.

A report from fixture data always carries the line: "Simulation on synthetic or model-priced data. This is not
evidence of profitability." A report on real paper data makes no performance statement until MIN_REAL_TRADES
completed paper trades exist, and then still shows the sample size, net-of-cost results and the largest loss.
"""
from collections import Counter
from decimal import Decimal

from . import journal as journal_mod

MIN_REAL_TRADES = 30           # configurable here; below this no performance claim is printed for real data
FIXTURE_CAVEAT = "Simulation on synthetic or model-priced data. This is not evidence of profitability."
REAL_SHORT_CAVEAT = ("Fewer than %d completed real paper trades: no performance claim is made. Sample size is shown "
                     "beside every figure.")
NOT_ADVICE = "PAPER trading only. Not financial advice."


def build_report(path, min_real_trades=MIN_REAL_TRADES):
    check = journal_mod.verify(path)
    records = journal_mod.read_records(path)
    sources = sorted({r.get("data_source") for r in records})
    report = {"path": path, "journal_ok": check.ok, "journal_problems": list(check.problems),
              "records": len(records), "data_sources": sources, "paper": True, "not_advice": NOT_ADVICE}
    if len(sources) > 1:
        report["error"] = "the journal mixes data sources; a report would mix fixture and real data, so none is built"
        return report
    source = sources[0] if sources else None
    report["data_source"] = source
    runs = [r for r in records if r["kind"] == "RUN_START"]
    decisions = [r for r in records if r["kind"] == "DECISION"]
    approvals = [r for r in records if r["kind"] == "APPROVAL"]
    exits = [r for r in records if r["kind"] == "EXIT"]
    criticals = [r for r in records if r["kind"] == "CRITICAL"]
    status = Counter(r["payload"]["status"] for r in decisions)
    net = [Decimal(r["payload"]["net_pnl_inr"]) for r in exits]
    reasons = Counter(r["payload"].get("exit_reason") for r in exits)
    block_reasons = Counter()
    for r in decisions:
        if r["payload"]["status"] == "BLOCKED":
            for code in r["payload"].get("risk", {}).get("reason_codes", []):
                block_reasons[code] += 1
    report.update({
        "runs": len(runs), "decisions": len(decisions), "decision_status": dict(sorted(status.items())),
        "blocked_reason_counts": dict(sorted(block_reasons.items())),
        "approvals_recorded": len(approvals), "approvals_accepted": sum(1 for r in approvals if r["payload"]["accepted"]),
        "completed_trades": len(exits), "exit_reasons": dict(sorted((str(k), v) for k, v in reasons.items())),
        "critical_exceptions": len(criticals),
        "net_pnl_inr": None, "largest_loss_inr": None, "wins": None, "losses": None, "sample_size": len(exits)})
    show_numbers = bool(exits) and (source == "fixture" or len(exits) >= min_real_trades)
    if show_numbers:
        report["net_pnl_inr"] = str(sum(net, Decimal(0)))
        report["largest_loss_inr"] = str(min(net)) if min(net) < 0 else "0"
        report["wins"], report["losses"] = sum(1 for n in net if n > 0), sum(1 for n in net if n < 0)
    if source == "fixture":
        report["caveat"] = FIXTURE_CAVEAT
        report["label"] = "PAPER - SIMULATED"
    elif source == "real":
        report["label"] = "PAPER - real market data, simulated fills"
        report["caveat"] = (REAL_SHORT_CAVEAT % min_real_trades) if len(exits) < min_real_trades else (
            "Sample of %d completed real paper trades. Results are net of simulated costs; past paper results do not "
            "predict future results." % len(exits))
    else:
        report["label"] = "PAPER"
        report["caveat"] = "The journal is empty."
    return report


def render_report(rep):
    lines = ["%s | %s" % (rep.get("label", "PAPER"), NOT_ADVICE), "journal: %s (%d records, chain %s)" % (
        rep["path"], rep["records"], "OK" if rep["journal_ok"] else "BROKEN: " + "; ".join(rep["journal_problems"]))]
    if "error" in rep:
        lines.append("ERROR: " + rep["error"])
        return "\n".join(lines)
    lines.append("data source: %s | runs: %d | decisions: %d" % (rep.get("data_source"), rep["runs"], rep["decisions"]))
    for k, v in rep["decision_status"].items():
        lines.append("  %-20s n=%d" % (k, v))
    if rep["blocked_reason_counts"]:
        lines.append("blocked by (counts): " + ", ".join("%s x%d" % kv for kv in rep["blocked_reason_counts"].items()))
    lines.append("approvals recorded: %d (accepted %d)" % (rep["approvals_recorded"], rep["approvals_accepted"]))
    lines.append("completed paper trades: n=%d" % rep["completed_trades"])
    if rep["net_pnl_inr"] is not None:
        lines.append("net P&L after simulated costs: %s INR (n=%d); largest loss: %s INR; wins %d, losses %d" % (
            rep["net_pnl_inr"], rep["sample_size"], rep["largest_loss_inr"], rep["wins"], rep["losses"]))
        lines.append("exit reasons: " + ", ".join("%s x%d" % kv for kv in rep["exit_reasons"].items()))
    else:
        lines.append("net P&L: not shown (n=%d)" % rep["sample_size"])
    if rep["critical_exceptions"]:
        lines.append("CRITICAL EXCEPTIONS: %d position(s) were still open when the session ended" % rep["critical_exceptions"])
    lines.append(rep["caveat"])
    return "\n".join(lines)
