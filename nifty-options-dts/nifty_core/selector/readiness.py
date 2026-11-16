"""Trade readiness: WHY the system has or has not traded, what it is waiting for, and where it would enter.

Everything here is derived from the selector's own evaluation (`Selection.evaluations`, the thresholds in
`SelectorParams`, the market analysis), never invented for display. The dashboard only renders what this module returns, so
the strategy logic exists in one place.

THE CHECKLIST STATUSES
    PASSED   the condition was checked now and holds
    FAILED   the condition was checked now and does NOT hold (it is checked again at every 5-minute slot)
    PENDING  not reached yet: it is only checked, or only matters, once the conditions above it pass
             (the option structure, the confirmation count, the risk gate, the fill)
    ERROR    a technical problem (stale or missing data, no usable expiry), not a statement about the market
    N/A      cannot apply (the NIFTY index has no traded volume)

THE READINESS STATUS (one of)
    WAITING FOR SIGNAL           the market has not met the conditions of any strategy yet
    WAITING FOR CONFIRMATION     a strategy qualifies (or nearly) and must hold for the confirmation slots first
    READY FOR RISK VALIDATION    a confirmed pick, before the risk gate has run
    READY TO EXECUTE             risk gate passed; only a human "yes" is missing (manual mode)
    NO VALID STRATEGY            the market suits none of the four strategies
    TRADE EXECUTED               the paper trade was entered
    EXECUTION BLOCKED            something other than the market stops a trade: a technical, safety or risk reason
READY TO EXECUTE is only ever set after the risk gate has approved. Nothing here promises a time or a price: execution depends on
live conditions and the final validations.

ENTRY PRICES are only shown when a real, quoted, liquid structure exists at the decision time, and they say when they were priced.
Otherwise the entry reads ENTRY NOT AVAILABLE with the reason; no price is made up.
"""
from decimal import Decimal

from . import analysis as A
from . import engine as E
from . import structures as S

WAITING_SIGNAL, WAITING_CONFIRMATION, READY_RISK, READY_EXECUTE = (
    "WAITING FOR SIGNAL", "WAITING FOR CONFIRMATION", "READY FOR RISK VALIDATION", "READY TO EXECUTE")
NO_VALID, EXECUTED, BLOCKED = "NO VALID STRATEGY", "TRADE EXECUTED", "EXECUTION BLOCKED"
STATUSES = (WAITING_SIGNAL, WAITING_CONFIRMATION, READY_RISK, READY_EXECUTE, NO_VALID, EXECUTED, BLOCKED)
PASSED, FAILED, PENDING, ERROR, NA = "PASSED", "FAILED", "PENDING", "ERROR", "N/A"
NOT_CONFIRMED = "NOT CONFIRMED"      # an optional confirmation that does not hold: it lowers confidence, it never blocks a trade

TECH_KIND = {
    "NO_ELIGIBLE_EXPIRY": "EXPIRY",
    "DATA_PROBLEM": "DATA", "CHAIN_UNAVAILABLE": "DATA", "CHAIN_FROM_FUTURE": "DATA", "CHAIN_STALE": "DATA",
    "DATA_UNVERIFIED": "DATA", "CHAIN_INVALID": "DATA", "CONTRACT_DATA_UNVERIFIED": "DATA", "SPOT_MISMATCH": "DATA",
    "REGIME_NOT_AT_DECISION_TIME": "DATA",
    "KILL_SWITCH": "SAFETY", "HIGH_IMPACT_EVENT": "SAFETY", "ABNORMAL_VOLATILITY": "SAFETY", "ONE_TRADE_AT_A_TIME": "SAFETY",
    "OUTSIDE_ENTRY_WINDOW": "TIMING", "TOO_EARLY": "TIMING", "MARKET_CLOSED": "TIMING"}
BLOCKING_KINDS = ("DATA", "EXPIRY", "SAFETY")
DIRECTIONAL = (E.DEBIT, E.CREDIT)
DISCLAIMER = "Execution depends on live market conditions and the final validations; no time or price is promised."
STEPS = ("Market signal", "Confirmation", "Risk check", "Executed")
VOLUME_NOTE = ("the NIFTY index has no traded volume, so there is no volume confirmation; option volume and open interest "
               "are checked under 'option structure and liquidity'")


def _p(x):
    return "{:,.2f}".format(Decimal(x))


def _slots(n):
    return "%d consecutive 5-minute slot%s" % (n, "" if n == 1 else "s")


def _s(x):
    return None if x is None else str(x)


LEGEND = {PASSED: "checked now and holds",
          FAILED: "checked now and does not hold (re-checked at every 5-minute slot)",
          PENDING: "not reached yet: only checked once the conditions above it pass",
          ERROR: "a technical problem, not a statement about the market", NA: "cannot apply to the index",
          NOT_CONFIRMED: "an optional confirmation that does not hold: it lowers confidence but never blocks a trade"}


# ---------------------------------------------------------------- per-strategy rows
def _cond_rows(ev):
    """The strategy's own conditions, as they were evaluated: required ones first, then the confirmations."""
    req, extra = [], []
    for c in ev.conditions:
        row = {"name": c.name, "detail": c.detail, "required": c.required, "weight": c.weight,
               "group": "market" if c.required else "extra",
               "status": PASSED if c.passed else (FAILED if c.required else NOT_CONFIRMED)}
        (req if c.required else extra).append(row)
    return req, extra


def _is_technical(ev):
    return any(b.name in ("option chain usable", "trading permitted now") for b in ev.blockers)


def _progress(ev):
    """(time window lost, share of required conditions that pass, not eligible): smaller sorts first = closer to trading."""
    req = [c for c in ev.conditions if c.required]
    lost = any(c.name == "time window" and not c.passed for c in req)
    done = sum(1 for c in req if c.passed)
    return (1 if lost else 0, -(Decimal(done) / Decimal(len(req)) if req else Decimal(0)), 0 if ev.eligible else 1)


def closest(evals, selected=None):
    """The evaluation to focus on: the selected one, else the one nearest to qualifying."""
    if selected is not None:
        return next((e for e in evals if e.strategy == selected), None)
    if not evals:
        return None
    return sorted(enumerate(evals), key=lambda ie: (_progress(ie[1]), ie[0]))[0][1]


def _strategy_row(ev, selected):
    req = [c for c in ev.conditions if c.required]
    failed = [c for c in req if not c.passed]
    if ev.strategy == selected:
        state = "SELECTED"
    elif ev.eligible:
        state = "QUALIFIES (below the minimum confidence)" if ev.confidence is not None else "QUALIFIES"
    elif _is_technical(ev):
        state = "BLOCKED (technical)"
    else:
        state = "NOT YET"
    return {"strategy": E.TITLES[ev.strategy], "state": state, "eligible": bool(ev.eligible and ev.proposal is not None),
            "confidence": ev.confidence, "required_total": len(req), "required_passed": len(req) - len(failed),
            "first_failed": None if not ev.blockers else {"name": ev.blockers[0].name, "detail": ev.blockers[0].detail},
            "failed_names": [c.name for c in failed] or ([ev.blockers[0].name] if ev.blockers else [])}


# ---------------------------------------------------------------- direction, zones, entry
def _direction(ev, a):
    if ev is None:
        return "NOT DETERMINED"
    if ev.proposal is not None and ev.proposal.direction:
        return "BULLISH" if ev.proposal.direction == "BULL" else "BEARISH"
    if ev.strategy in DIRECTIONAL:
        if a is not None and a.structure == A.TRENDING_BULLISH:
            return "BULLISH"
        if a is not None and a.structure == A.TRENDING_BEARISH:
            return "BEARISH"
        return "NOT DETERMINED (no trend yet)"
    return "NEUTRAL"


def _zones(ev, a, p):
    """The price levels the strategy's conditions are measured against, computed from the actual thresholds."""
    if ev is None or a is None:
        return []
    out = []
    if ev.strategy in DIRECTIONAL:
        up = a.day_open * (1 + p.trend_net_move_pct / 100)
        down = a.day_open * (1 - p.trend_net_move_pct / 100)
        want = []
        if a.structure != A.TRENDING_BEARISH:
            want.append(("Bullish trend level", up, "NIFTY must hold above this level (a net move of at least +%s%% from the day open %s), "
                         "above the day average %s, with efficiency of at least %s" % (
                             p.trend_net_move_pct, _p(a.day_open), _p(a.day_average), p.trend_efficiency_min)))
        if a.structure != A.TRENDING_BULLISH:
            want.append(("Bearish trend level", down, "NIFTY must hold below this level (a net move of at least -%s%% from the day open %s), "
                         "below the day average %s, with efficiency of at least %s" % (
                             p.trend_net_move_pct, _p(a.day_open), _p(a.day_average), p.trend_efficiency_min)))
        out += [{"label": l, "price": _p(v), "rule": r} for l, v, r in want]
        if a.resistance is not None:
            out.append({"label": "Nearest resistance", "price": _p(a.resistance),
                        "rule": "the next level above spot; a debit spread wants at least %s%% of room before it" % p.room_min_pct})
        if a.support is not None:
            out.append({"label": "Nearest support", "price": _p(a.support), "rule": "the next level below spot"})
    elif ev.strategy == E.CONDOR:
        span = a.day_high - a.day_low
        out.append({"label": "Mid-range band", "low": _p(a.day_low + p.range_mid_low * span), "high": _p(a.day_low + p.range_mid_high * span),
                    "rule": "spot must sit between %s%% and %s%% of today's range %s - %s" % (
                        p.range_mid_low * 100, p.range_mid_high * 100, _p(a.day_low), _p(a.day_high))})
        out.append({"label": "Range-bound band", "low": _p(a.day_open * (1 - p.range_net_move_max_pct / 100)),
                    "high": _p(a.day_open * (1 + p.range_net_move_max_pct / 100)),
                    "rule": "net move from the day open must stay within +/-%s%% (efficiency at most %s, day range at most %s%%)" % (
                        p.range_net_move_max_pct, p.range_efficiency_max, p.range_day_range_max_pct)})
    else:
        out.append({"label": "Pin band", "low": _p(a.day_average * (1 - p.fly_pin_band_pct / 100)),
                    "high": _p(a.day_average * (1 + p.fly_pin_band_pct / 100)),
                    "rule": "spot must sit within %s%% of the day average %s, on a day range of at most %s%%" % (
                        p.fly_pin_band_pct, _p(a.day_average), p.fly_day_range_max_pct)})
    return out


def _enforced_trigger(trig, p):
    """The entry conditions that the live logic really enforces. The selector's narrative also lists an 'entry candle' rule and a
    'spot within 0.15% of the decision spot' rule (its first two lines); the live pipeline does not check either (the pick has
    to hold for the confirmation slots instead), so they are left out rather than shown as if they were required."""
    return (["Every required condition above passes at the decision slot",
             "The same strategy is the pick at %s (the confirmation)" % _slots(p.entry_confirmations)]
            + list(trig[2:]))


def _entry(ev, narrative, a, p, cfg):
    """Where the trade would be entered, or ENTRY NOT AVAILABLE with the reason. Never an invented price."""
    if ev is None or a is None:
        return {"available": False, "label": "ENTRY NOT AVAILABLE", "reason": "the market cannot be read yet", "watch": []}
    credit = ev.strategy in (E.CREDIT, E.CONDOR, E.FLY)
    latest = p.latest_entry_credit_ist if credit else p.latest_entry_debit_ist
    hold = ("a pick is entered at the first slot where it qualifies" if p.entry_confirmations == 1 else
            "a pick must hold for %s before entry, so the earliest entry is about %d minutes after the first qualifying slot" % (
                _slots(p.entry_confirmations), 5 * (p.entry_confirmations - 1)))
    timeframe = ("Decisions run at every 5-minute slot; %s. No new %s entry after %s IST; every position is closed by %s IST. "
                 "Execution depends on live market conditions and the final validations." % (
                     hold, "credit" if credit else "debit-spread", latest, cfg.flat_by_ist))
    if ev.proposal is None or not narrative:
        reason = (ev.blockers[0].name + ": " + ev.blockers[0].detail) if ev.blockers else "no structure was built"
        return {"available": False, "label": "ENTRY NOT AVAILABLE", "reason": reason, "watch": _zones(ev, a, p),
                "timeframe": timeframe,
                "note": "No strike or premium is shown because none is valid yet: a price is only shown for a real, quoted, liquid structure."}
    setup, trig, exits = narrative["setup"], narrative["trigger"], narrative["exit_rules"]
    net_key = "net_credit_points" if "net_credit_points" in setup else "net_debit_points"
    units = Decimal(setup["units"])
    return {"available": True, "label": "ENTRY (indicative, at the quotes of the decision time)",
            "underlying": _p(a.spot), "underlying_basis": "close of the last completed 5-minute candle",
            "priced_at": a.as_of.isoformat(),
            "legs": [{"action": l["action"], "type": l["type"], "strike": l["strike"], "price": l["price"], "bid": l["bid"],
                      "ask": l["ask"], "contract": l["contract"]} for l in setup["legs"]],
            "net": {"kind": "credit" if net_key == "net_credit_points" else "debit", "points": setup[net_key],
                    "inr": str((Decimal(setup[net_key]) * units).quantize(Decimal("0.01")))},
            "expiry": setup["expiry"], "units": setup["units"], "width_points": setup["width_points"],
            "max_profit_inr": setup["max_profit_inr"], "max_loss_inr": setup["max_loss_inr"],
            "worst_case_inr": setup["worst_case_with_costs_inr"], "risk_limit_inr": setup["risk_limit_inr"],
            "reward_to_risk": setup["reward_to_risk"], "breakevens": setup["breakevens_at_expiry"],
            "trigger": _enforced_trigger(trig, p), "stop_loss": exits["stop_loss"], "target": exits["target"],
            "early_exit": list(exits["early_exit"]), "time_exit": exits["time_exit"], "timeframe": timeframe,
            "note": "Priced from the live chain at the decision time; it is re-quoted and re-validated at the moment of entry, so the "
                    "fill can differ and the trade can still be refused."}


# ---------------------------------------------------------------- the checklist of the focus strategy
def _gate_row(issues):
    """The first row of every checklist: data, expiry and safety gates that are not about the market."""
    blocking = [i for i in issues if i["kind"] in BLOCKING_KINDS]
    timing = [i for i in issues if i["kind"] == "TIMING"]
    if blocking:
        return {"name": "data, expiry and safety gates", "required": True, "weight": 0, "status": ERROR, "group": "gate",
                "detail": "; ".join(i["text"] for i in blocking)}
    if timing:
        return {"name": "data, expiry and safety gates", "required": True, "weight": 0, "status": FAILED, "group": "gate",
                "detail": "; ".join(i["text"] for i in timing)}
    return {"name": "data, expiry and safety gates", "required": True, "weight": 0, "status": PASSED, "group": "gate",
            "detail": "fresh option chain, an expiry far enough away, no scheduled high-impact event, no kill file, inside the entry window"}


def _checklist(ev, confirm, p, cfg, issues=()):
    if ev is None:
        return [_gate_row(issues)]
    req, extra = _cond_rows(ev)
    technical = _is_technical(ev)
    for row in req:
        if row["name"] in ("option chain usable", "trading permitted now"):
            row["status"] = ERROR
    market_ok = all(r["status"] == PASSED for r in req)
    rows = [_gate_row(issues)] + list(req)
    rows.append({"name": "volume confirmation", "required": False, "weight": 0, "status": NA, "group": "extra", "detail": VOLUME_NOTE})
    if ev.proposal is not None:
        rows.append({"name": "option structure and liquidity", "required": True, "status": PASSED, "weight": 0,
                     "detail": "a quoted structure exists: both sides of every leg quoted, spreads, volume, open interest and depth within limits"})
        rows.append({"name": "worst case within the risk limit (pre-check)", "required": True, "status": PASSED, "weight": 0,
                     "detail": "worst case INR %s (loss + costs + slippage) against the INR %s per-trade limit" % (
                         _p(ev.proposal.defined_risk_inr), _p(cfg.risk_per_trade_inr))})
    elif technical:
        why = next(b.detail for b in ev.blockers if b.name in ("option chain usable", "trading permitted now"))
        rows.append({"name": "option structure and liquidity", "required": True, "status": ERROR, "weight": 0,
                     "detail": "cannot be checked: %s" % why})
        rows.append({"name": "worst case within the risk limit (pre-check)", "required": True, "status": PENDING, "weight": 0,
                     "detail": "checked once a structure exists"})
    elif market_ok and ev.blockers:
        rows.append({"name": "option structure and liquidity", "required": True, "status": FAILED, "weight": 0,
                     "detail": ev.blockers[0].detail})
        rows.append({"name": "worst case within the risk limit (pre-check)", "required": True, "status": PENDING, "weight": 0,
                     "detail": "checked once a structure exists"})
    else:
        rows.append({"name": "option structure and liquidity", "required": True, "status": PENDING, "weight": 0,
                     "detail": "checked once the conditions above pass"})
        rows.append({"name": "worst case within the risk limit (pre-check)", "required": True, "status": PENDING, "weight": 0,
                     "detail": "checked once a structure exists"})
    need = p.entry_confirmations
    if confirm and ev.proposal is not None and confirm.get("have") is not None:
        have = confirm["have"]
        rows.append({"name": "confirmation: same pick at %s" % _slots(need), "required": True, "weight": 0,
                     "status": PASSED if have >= need else PENDING,
                     "detail": "%d of %d slots so far%s" % (have, need, "" if have >= need else ": it must stay the pick at the next slot")})
    else:
        rows.append({"name": "confirmation: same pick at %s" % _slots(need), "required": True, "weight": 0,
                     "status": PENDING, "detail": "starts once the strategy qualifies"})
    rows.append({"name": "risk gate and paper execution", "required": True, "weight": 0, "status": PENDING,
                 "detail": "the per-trade and daily limits, trade count, kill file and a fresh quote for every leg are checked at entry"})
    for r in rows:
        r.setdefault("group", "execution")                 # the option structure, risk pre-check, confirmation, risk gate
    return rows + extra


# ---------------------------------------------------------------- the whole readiness record
def _technical_issues(sel):
    out = []
    for r in sel.reasons:
        kind = TECH_KIND.get(r.code)
        if kind:
            out.append({"kind": kind, "code": r.code, "text": r.text})
    return out


def _next_action(status, focus, entry, issues, a, confirm, p, extra=None):
    """The next action in plain language, WITHOUT the disclaimer (that is its own field)."""
    name = E.TITLES[focus.strategy] if focus is not None else None
    tail = ""
    if status == EXECUTED:
        return "A paper trade was entered (%s). It is managed automatically; see the open position above." % name
    if status == BLOCKED and extra:
        return "Signal confirmed for %s, but it was not executed: %s." % (name, extra) + tail
    if status == BLOCKED:
        hard = [i for i in issues if i["kind"] in BLOCKING_KINDS]
        text = "; ".join(i["text"] for i in hard) or "a technical or safety condition"
        return ("No trade can be entered right now because of a non-market condition: %s. "
                "The market-side conditions are still shown below and re-checked every 5 minutes." % text) + tail
    if status == READY_EXECUTE:
        return "Every condition and the risk gate passed for %s. Waiting for your 'yes' on the ticket (manual mode)." % name + tail
    if status == READY_RISK:
        return "%s is confirmed. The risk gate and fresh quotes are validated next." % name + tail
    if status == WAITING_CONFIRMATION:
        if confirm and confirm.get("have") is not None:
            return ("%s qualifies now. It must stay the pick for %s (%d so far); the next check is at the next slot. "
                    "After that the system validates strikes, premiums and risk before entering." % (name, _slots(p.entry_confirmations), confirm["have"])) + tail
        return "%s has every required condition but the confidence is below the minimum of %d; waiting for it to improve." % (name, p.min_confidence) + tail
    if status == NO_VALID:
        return ("Market conditions are unsuitable for all four strategies right now (%s). "
                "They are re-read at every 5-minute slot; nothing is forced." % (a.summary if a is not None else "not readable yet")) + tail
    if focus is None:
        return "Waiting for enough candles to read the market." + tail
    wait = [c for c in focus.conditions if c.required and not c.passed and c.name not in ("option chain usable", "trading permitted now")]
    if wait:
        first = wait[0]
        more = " (%d more required condition%s)" % (len(wait) - 1, "" if len(wait) == 2 else "s") if len(wait) > 1 else ""
        zone = ""
        if entry and entry.get("watch"):
            z = entry["watch"][0]
            zone = " Watch: %s %s." % (z["label"], z["price"] if "price" in z else "%s - %s" % (z["low"], z["high"]))
        return ("Closest to qualifying: %s. Waiting for: %s, %s%s.%s After the conditions pass the system validates option strikes, "
                "premiums and risk before entering." % (name, first.name, first.detail, more, zone)) + tail
    return "Waiting for the next 5-minute check." + tail


def build(sel, p, cfg, confirm=None):
    """The readiness of one selector decision. `confirm` = {"have": n} (slots the pick has held), or None when the decision is not
    part of a confirmation run (for example the select-strategy command). JSON-safe."""
    a, evals = sel.analysis, sel.evaluations
    issues = _technical_issues(sel)
    focus = closest(evals, sel.selected if sel.outcome == E.TRADE else None)
    narrative = (sel.narratives or {}).get(focus.strategy) if focus is not None else None
    entry = _entry(focus, narrative, a, p, cfg)
    if sel.outcome == E.TRADE:
        status = WAITING_CONFIRMATION if (confirm and confirm.get("have") is not None and confirm["have"] < p.entry_confirmations) else READY_RISK
    elif any(i["kind"] in BLOCKING_KINDS for i in issues) or any(_is_technical(e) for e in evals):
        status = BLOCKED
    elif a is None or any(i["kind"] == "TIMING" for i in issues):
        status = WAITING_SIGNAL
    elif a.structure == A.UNCLEAR or (evals and all(_progress(e)[0] == 1 for e in evals)):
        status = NO_VALID
    elif any(e.eligible for e in evals):
        status = WAITING_CONFIRMATION
    else:
        status = WAITING_SIGNAL
    main_action = _next_action(status, focus, entry, issues, a, confirm, p)
    rows = _checklist(focus, confirm, p, cfg, issues)
    counts = {k: sum(1 for r in rows if r["required"] and r["status"] == k) for k in (PASSED, FAILED, PENDING, ERROR)}
    return {
        "status": status,
        "regime": {"structure": a.structure if a is not None else None, "summary": a.summary if a is not None else
                   "; ".join(r.text for r in sel.reasons) or "not readable yet",
                   "spot": _s(a.spot) if a is not None else None, "as_of": sel.as_of.isoformat(),
                   "day_open": _s(a.day_open) if a is not None else None, "day_high": _s(a.day_high) if a is not None else None,
                   "day_low": _s(a.day_low) if a is not None else None, "day_average": _p(a.day_average) if a is not None else None},
        "evaluated_strategy": E.TITLES[focus.strategy] if focus is not None else None,
        "evaluated_selected": bool(sel.outcome == E.TRADE),
        "direction": _direction(focus, a),
        "confidence": focus.confidence if focus is not None else None,
        "entry": entry, "checklist": rows, "counts": counts,
        "strategies": [_strategy_row(e, sel.selected if sel.outcome == E.TRADE else None) for e in evals],
        "technical": issues,
        "next_action_main": main_action, "next_action": main_action + " " + DISCLAIMER, "disclaimer": DISCLAIMER,
        "progress": _stepper(status, "technical" if status == BLOCKED else None),
        "confirmation": ({"have": confirm.get("have"), "need": p.entry_confirmations} if confirm else None),
    }


def finalize(ready, status, record):
    """Fold what happened after the selector (risk gate, ticket, fill) into the readiness. Mutates and returns it.

    status is the decision's final status: ENTERED, BLOCKED, AWAITING_APPROVAL, ENTRY_NOT_FILLED, DECLINED, TICKET_EXPIRED, or
    anything else (unchanged). READY TO EXECUTE is only set when the risk gate has approved."""
    if not ready:
        return ready
    rows = ready.get("checklist") or []
    gate_row = next((r for r in rows if r["name"] == "risk gate and paper execution"), None)
    risk = (record or {}).get("risk") or {}
    why = None
    if status == "ENTERED":
        ready["status"] = EXECUTED
        if gate_row:
            gate_row.update(status=PASSED, detail="approved by the risk gate and filled (simulated)")
    elif status == "AWAITING_APPROVAL":
        ready["status"] = READY_EXECUTE
        if gate_row:
            gate_row.update(status=PASSED, detail="approved by the risk gate; waiting for the human yes")
    elif status == "BLOCKED":
        ready["status"] = BLOCKED
        why = "risk validation failed (%s)" % ("; ".join(r["text"] for r in risk.get("reasons", [])) or "see the journal")
        if gate_row:
            gate_row.update(status=FAILED, detail=why)
    elif status in ("ENTRY_NOT_FILLED", "DECLINED", "TICKET_EXPIRED"):
        ready["status"] = BLOCKED
        why = {"ENTRY_NOT_FILLED": "the simulated fill was refused (%s)" % "; ".join(
            r["text"] for r in (record or {}).get("reasons_not_filled", [])),
               "DECLINED": "the ticket was declined", "TICKET_EXPIRED": "the ticket expired unanswered"}[status]
        if gate_row:
            gate_row.update(status=FAILED, detail=why)
    elif status == "NO_TRADE" and ready["status"] == READY_RISK:
        ready["status"] = WAITING_SIGNAL
    ready["counts"] = {k: sum(1 for r in rows if r["required"] and r["status"] == k) for k in (PASSED, FAILED, PENDING, ERROR)}
    if status in ("ENTERED", "AWAITING_APPROVAL", "BLOCKED", "ENTRY_NOT_FILLED", "DECLINED", "TICKET_EXPIRED"):
        ready["next_action_main"] = _finished_action(ready, status, why)
        ready["next_action"] = ready["next_action_main"] + (" " + DISCLAIMER if status != "ENTERED" else "")
        ready["disclaimer"] = "" if status == "ENTERED" else DISCLAIMER
    ready["progress"] = _stepper(ready["status"], {"BLOCKED": "risk", "ENTRY_NOT_FILLED": "fill", "DECLINED": "fill",
                                                    "TICKET_EXPIRED": "fill"}.get(status))
    ready["final_status"] = status
    return ready


def _finished_action(ready, status, why):
    name = ready.get("evaluated_strategy")
    if status == "ENTERED":
        return "A paper trade was entered (%s). It is managed automatically; see the open position above." % name
    if status == "AWAITING_APPROVAL":
        return "Every condition and the risk gate passed for %s. Waiting for your 'yes' on the ticket (manual mode)." % name
    return "Signal confirmed for %s, but it was not executed: %s." % (name, why)


def _stepper(status, blocked_at=None):
    """Where the decision is in the path signal -> confirmation -> risk check -> executed, for the progress stepper.
    state: waiting (the current step is not done yet), blocked (it cannot proceed), idle (nothing to do), done."""
    at = {WAITING_SIGNAL: 0, NO_VALID: 0, WAITING_CONFIRMATION: 1, READY_RISK: 2, READY_EXECUTE: 3, EXECUTED: len(STEPS),
          BLOCKED: 0}.get(status, 0)
    state = "done" if status == EXECUTED else "idle" if status == NO_VALID else "blocked" if status == BLOCKED else "waiting"
    if status == BLOCKED and blocked_at == "risk":
        at = 2
    elif status == BLOCKED and blocked_at == "fill":
        at = 3
    return {"steps": list(STEPS), "current": at, "state": state}


# ---------------------------------------------------------------- history of decisions
def history_entry(record):
    """One compact row of the decision history from a journaled DECISION record that carries a readiness block."""
    ready = (record or {}).get("readiness")
    if not ready:
        return None
    rows = ready.get("checklist") or []
    failed = [r["name"] for r in rows if r["required"] and r["status"] in (FAILED, ERROR)]
    pending = [r["name"] for r in rows if r["required"] and r["status"] == PENDING]
    reasons = [x.get("text") for x in (record.get("strategy") or {}).get("reasons", [])]
    return {"at": record.get("decision_time"), "regime": (ready.get("regime") or {}).get("structure"),
            "strategy": ready.get("evaluated_strategy"), "direction": ready.get("direction"), "status": ready.get("status"),
            "price": (ready.get("regime") or {}).get("spot"),
            "expected": [("%s %s" % (l["action"], l["strike"] + l["type"])) for l in (ready.get("entry") or {}).get("legs", [])]
            or [z.get("label") for z in (ready.get("entry") or {}).get("watch", [])],
            "passed": (ready.get("counts") or {}).get(PASSED, 0), "failed_names": failed[:4], "pending_count": len(pending),
            "reason": (reasons[0] if reasons else (ready.get("next_action") or ""))[:240],
            "traded": record.get("status") == "ENTERED",
            "qualifying": [s["strategy"] for s in ready.get("strategies", []) if s.get("eligible")],
            "later_valid_at": None}


def record_in_history(history, entry, keep=200):
    """Append `entry` and mark earlier rows whose strategy became valid at this decision. Mutates and returns `history`."""
    if entry is None:
        return history
    for old in history:
        if old["later_valid_at"] is None and old["strategy"] in entry["qualifying"] and old["at"] < entry["at"] \
                and old["strategy"] not in old["qualifying"]:
            old["later_valid_at"] = entry["at"]
    history.append(entry)
    del history[:-keep]
    return history


def history_from_records(records):
    """Rebuild the day's decision history from journal records (used when a session restarts mid-day)."""
    history = []
    for r in records:
        if r.get("kind") == "DECISION" and not r["payload"].get("follow_up"):
            record_in_history(history, history_entry(r["payload"]))
    return history
