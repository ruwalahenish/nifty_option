"""Render a Selection as the trade-decision report: Market Condition, Selected Strategy, Why, Trigger, Setup, Exit Rules,
Confidence. Also a JSON-friendly form for the journal."""
from . import analysis as A
from . import engine as E

HEADER = "STRATEGY SELECTION | PAPER TRADING ONLY | proposal, not an order | Not financial advice"


def _condition_lines(ev):
    out = []
    for c in ev.conditions:
        mark = "PASS" if c.passed else "FAIL"
        out.append("      [%s] %s%s: %s" % (mark, c.name, "" if c.required else " (confirmation, weight %d)" % c.weight, c.detail))
    return out


def render(sel, show_all=True):
    a = sel.analysis
    L = [HEADER, "Decision time: %s IST" % sel.as_of.strftime("%Y-%m-%d %H:%M"), ""]
    L.append("MARKET CONDITION")
    if a is None:
        L.append("  not readable yet (see the reasons below)")
    else:
        L += ["  %s" % a.summary,
              "  spot %s | day open %s | high %s | low %s | day average %s" % (a.spot, a.day_open, a.day_high, a.day_low, A._p(a.day_average)),
              "  support %s | resistance %s | opening range %s - %s" % (
                  a.support if a.support is not None else "none below", a.resistance if a.resistance is not None else "none above",
                  a.opening_low, a.opening_high)]
    L.append("")
    if sel.outcome == E.NO_TRADE:
        L += ["SELECTED STRATEGY", "  NO TRADE", "", "WHY NO TRADE"]
        L += ["  - %s" % r.text for r in sel.reasons]
        L += ["", "  Nothing is forced: capital is protected by staying out. The market is re-read at the next check."]
    else:
        s = sel.setup
        L += ["SELECTED STRATEGY", "  %s  (%s)" % (E.TITLES[sel.selected], s["strategy"]), "", "WHY THIS STRATEGY"]
        L += ["  - %s" % w for w in sel.why]
        L += ["", "TRADE TRIGGER (all must hold at entry; otherwise re-evaluate)"]
        L += ["  %d. %s" % (i, t) for i, t in enumerate(sel.trigger, 1)]
        L += ["", "TRADE SETUP", "  expiry %s | %d lot x %d = %d units" % (s["expiry"], s["lots"], s["lot_size"], s["units"])]
        L += ["  %-4s %s  strike %s %s @ %s (bid %s / ask %s)" % (
            l["action"], l["contract"], l["strike"], l["type"], l["price"], l["bid"], l["ask"]) for l in s["legs"]]
        net = ("net credit %s pts" % s["net_credit_points"]) if "net_credit_points" in s else ("net debit %s pts" % s["net_debit_points"])
        L += ["  %s | width %s pts" % (net, s["width_points"]),
              "  max profit INR %s | max loss INR %s | worst case incl. costs and slippage INR %s (limit INR %s)" % (
                  s["max_profit_inr"], s["max_loss_inr"], s["worst_case_with_costs_inr"], s["risk_limit_inr"]),
              "  reward-to-risk %s | breakevens at expiry %s | entry friction %s%%" % (
                  s["reward_to_risk"], ", ".join(s["breakevens_at_expiry"]), s["entry_friction_pct"]),
              "  (%s)" % s["figures_note"]]
        x = sel.exit_rules
        L += ["", "EXIT RULES", "  target     : %s" % x["target"], "  stop-loss  : %s" % x["stop_loss"], "  time exit  : %s" % x["time_exit"],
              "  early exit : any of"]
        L += ["      - %s" % e for e in x["early_exit"]]
        L += ["", "CONFIDENCE", "  %s (%d/100): %d base for all required conditions met + up to %d for the confirmations that agree. "
               "It counts agreeing checks; it is not a probability of profit." % (
                   sel.confidence_label, sel.confidence, E.BASE_CONFIDENCE, E.CONFIRMATION_SHARE)]
    if show_all and sel.evaluations:
        L += ["", "HOW EACH STRATEGY WAS JUDGED"]
        for ev in sel.evaluations:
            status = ("ELIGIBLE, confidence %d" % ev.confidence) if ev.eligible else "NOT eligible"
            chosen = "  <-- selected" if ev.strategy == sel.selected else ""
            L.append("  %s: %s%s" % (E.TITLES[ev.strategy], status, chosen))
            L += _condition_lines(ev)
    L += ["", E.NOTE]
    return "\n".join(L)


def to_json(sel):
    return {
        "outcome": sel.outcome, "as_of": sel.as_of.isoformat(),
        "market_condition": A.analysis_to_json(sel.analysis) if sel.analysis is not None else None,
        "selected_strategy": sel.selected, "confidence": sel.confidence, "confidence_label": sel.confidence_label,
        "why": list(sel.why), "trade_trigger": list(sel.trigger), "trade_setup": sel.setup, "exit_rules": sel.exit_rules,
        "reasons": [{"code": r.code, "text": r.text} for r in sel.reasons],
        "evaluations": [{"strategy": e.strategy, "eligible": e.eligible, "confidence": e.confidence,
                         "conditions": [{"name": c.name, "passed": c.passed, "required": c.required, "weight": c.weight,
                                         "detail": c.detail} for c in e.conditions]} for e in sel.evaluations],
        "regime_label": sel.regime_label, "thresholds_fingerprint": sel.thresholds_fingerprint, "note": sel.note,
    }
