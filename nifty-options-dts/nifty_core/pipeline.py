"""The end-to-end paper pipeline for one trading day, replayed from a read-only provider.

    data -> regime -> strategy -> risk -> TICKET -> human "yes" -> paper entry -> position management -> journal

Every tick is journaled where something was decided: NO TRADE and BLOCKED are journaled like trades. Without a
recorded "yes" for the exact ticket nothing is entered (AWAITING_APPROVAL). The replay applies the supplied answer at the
decision timestamp; a real-time flow would have to re-run the pipeline on fresh data after the human replies, because an
approval goes stale after the staleness limit.

No part of this module places a real order: it only uses the paper executor and the read-only provider.
"""
import datetime
import hashlib
import json
from collections import Counter, namedtuple
from decimal import Decimal

from . import approval as approval_mod
from .contracts import ContractSpecs, hhmm
from .costs.engine import load_cost_engine
from .errors import RiskViolation
from .execution import paper as paper_mod
from .guards import paper_only
from .journal import journal as journal_mod
from .market.calendar_nse import NseCalendar
from .market.ist import ist_datetime
from .positions import manager as manager_mod
from .positions import params as position_params
from .regime import params as regime_params
from .risk import engine as risk_engine
from .strategy import engine as strategy_engine
from .strategy import params as strategy_params

TICK_MINUTES = 5
Context = namedtuple("Context", "cfg specs calendar cost_engine strategy_params regime_params position_params data_mode")
DaySummary = namedtuple("DaySummary", (
    "day status_counts entries exits criticals net_pnl_inr tickets_awaiting final_ledger journal_path"))


def load_context(cfg, data_mode="fixture"):
    """Everything the pipeline needs except the provider, built from the versioned files."""
    specs = ContractSpecs.load()
    return Context(cfg, specs, NseCalendar.load(specs=specs, data_mode=data_mode), load_cost_engine(data_mode=data_mode),
                   strategy_params.load_params(), regime_params.load_params(), position_params.load_params(), data_mode)


def _reason_list(reasons):
    return [{"code": r.code, "text": r.text} for r in reasons]


def _inputs_hash(provider, decision, ctx):
    scenario = getattr(getattr(provider, "scenario", None), "name", "provider")
    blob = {"scenario": scenario, "as_of": decision.decision_time.isoformat(),
            "candles_used": decision.regime.candles_used if decision.regime is not None else None,
            "chain_snapshot": decision.candidate.chain_snapshot_time.isoformat() if decision.candidate else None,
            "regime_fp": ctx.regime_params.fingerprint, "strategy_fp": ctx.strategy_params.fingerprint}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode("utf-8")).hexdigest()


def tick_times(ctx, day):
    """Decision/management times: the entry-window start to the session close, every five minutes (IST)."""
    session = ctx.calendar.session(day)
    if session is None:
        return []
    t = ist_datetime(day, hhmm(ctx.cfg.entry_window_start_ist))
    end = ist_datetime(day, session.close)
    out = []
    while t <= end:
        out.append(t)
        t += datetime.timedelta(minutes=TICK_MINUTES)
    return out


def run_day(provider, day, ctx, kill_dir, journal, answers=None):
    """Replay one trading day. `answers` maps a ticket fingerprint to the human's reply, word for word.

    Returns a DaySummary. Nothing is entered without an accepted reply for that exact ticket.
    """
    paper_only.assert_paper_only(ctx.cfg)
    answers = dict(answers or {})
    cfg = ctx.cfg
    ticks = tick_times(ctx, day)
    if not ticks:
        raise ValueError("no trading session on %s" % day)
    mgr = risk_engine.RiskManager(cfg, ctx.cost_engine, ctx.specs, ctx.calendar, kill_dir, day)
    ex = paper_mod.PaperExecutor(cfg, ctx.cost_engine, ctx.specs, ctx.calendar, mgr, provider)
    pm = manager_mod.PositionManager(cfg, ctx.position_params, ex, mgr, provider, ctx.calendar, ctx.regime_params,
                                     ctx.cost_engine, data_mode=ctx.data_mode)
    journal.append("RUN_START", ticks[0], {
        "notice": "PAPER trading only, SIMULATED fills. Not financial advice.", "day": day.isoformat(),
        "data_mode": ctx.data_mode, "config_fingerprint": risk_engine.config_fingerprint(cfg),
        "strategy_thresholds": ctx.strategy_params.fingerprint, "regime_thresholds": ctx.regime_params.fingerprint,
        "position_thresholds": ctx.position_params.fingerprint, "answers_supplied": len(answers),
        "scenario": getattr(getattr(provider, "scenario", None), "name", None)})
    status_counts, awaiting, entries, locked = Counter(), [], 0, False
    journaled_exit = set()
    for t in ticks:
        # 1 manage open positions first
        for res in pm.manage_all(t):
            _journal_position(journal, t, res, pm, journaled_exit)
        snap = mgr.snapshot()
        if t.time() > hhmm(cfg.entry_window_end_ist) or t.time() >= hhmm(cfg.flat_by_ist):
            continue
        if pm.active():
            continue
        if snap.trades_today >= cfg.max_trades_per_day or snap.realized_loss_used_inr >= cfg.max_daily_loss_inr:
            if not locked:
                locked = True
                journal.append("DAY_LOCKED", t, {"reason": "trade count or daily loss limit reached",
                                                   "trades_today": snap.trades_today,
                                                   "realized_loss_used_inr": str(snap.realized_loss_used_inr)})
            continue
        # 2 one decision
        status = _decide(provider, ctx, mgr, pm, journal, t, answers, awaiting)
        status_counts[status] += 1
        if status == "ENTERED":
            entries += 1
    pm.end_of_session(ticks[-1])
    for exc in pm.critical_exceptions:
        journal.append("CRITICAL", ticks[-1], {"position_id": exc.position_id, "text": exc.text})
    closed = [p for p in pm.positions() if p.state == manager_mod.CLOSED]
    exits = len(closed)
    net = sum((Decimal(pm.pnl_report(p.position_id)["net_realized_pnl_inr"]) for p in closed), Decimal(0))
    snap = mgr.snapshot()
    summary = DaySummary(day, dict(sorted(status_counts.items())), entries, exits, len(pm.critical_exceptions), net,
                         tuple(awaiting), snap, journal.path)
    journal.append("RUN_END", ticks[-1], {
        "status_counts": summary.status_counts, "entries": entries, "completed_trades": exits,
        "critical_exceptions": summary.criticals, "net_pnl_inr": str(net), "tickets_awaiting": list(awaiting),
        "ledger": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in snap._asdict().items()},
        "caveat": "Simulation on synthetic or model-priced data. This is not evidence of profitability."
        if journal.data_source == "fixture" else "Paper results on recorded data."})
    return summary


def _decide(provider, ctx, mgr, pm, journal, t, answers, awaiting):
    d = strategy_engine.decide_at(provider, ctx.calendar, ctx.specs, ctx.cfg, ctx.strategy_params,
                                  ctx.regime_params, t, ctx.data_mode)
    record = {"decision_time": t.isoformat(), "inputs_hash": _inputs_hash(provider, d, ctx),
              "regime": {"label": d.regime.label, "evidence_score": None if d.regime.evidence_score is None
                         else str(d.regime.evidence_score), "bull_votes": d.regime.bull_votes,
                         "bear_votes": d.regime.bear_votes, "reasons": _reason_list(d.regime.reasons),
                         "thresholds_fingerprint": d.regime.thresholds_fingerprint},
              "strategy": {"outcome": d.outcome, "reasons": _reason_list(d.reasons),
                           "candidates_tested": len(d.considered),
                           "thresholds_fingerprint": d.thresholds_fingerprint}}
    if d.outcome != strategy_engine.CANDIDATE:
        record["status"] = "NO_TRADE"
        journal.append("DECISION", t, record)
        return "NO_TRADE"
    cand = d.candidate
    record["strategy"]["candidate"] = strategy_engine.candidate_to_journal(cand)
    rd = mgr.request_entry(cand, t)
    record["risk"] = {"outcome": rd.outcome, "reason_codes": [r.code for r in rd.reasons],
                      "reasons": _reason_list(rd.reasons), "ledger_before": _snap(rd.ledger_before),
                      "total_defined_risk_inr": None if rd.calculation is None
                      else str(rd.calculation["total_defined_risk_inr"]),
                      "per_trade_limit_inr": rd.limits["risk_per_trade_inr"], "fingerprints": rd.fingerprints}
    if rd.outcome != risk_engine.APPROVED:
        record["status"] = "BLOCKED"
        journal.append("DECISION", t, record)
        return "BLOCKED"
    ticket = approval_mod.make_ticket(cand, rd)
    journal.append("TICKET", t, {"fingerprint": ticket.fingerprint, "text": ticket.text,
                                  "approval_id": rd.approval.approval_id})
    reply = answers.get(ticket.fingerprint)
    if reply is None:
        awaiting.append(ticket.fingerprint)
        _release(mgr, rd.approval)
        record["status"] = "AWAITING_APPROVAL"
        record["ticket_fingerprint"] = ticket.fingerprint
        journal.append("DECISION", t, record)
        return "AWAITING_APPROVAL"
    ok, why = approval_mod.parse_yes(reply, ticket)
    journal.append("APPROVAL", t, {"fingerprint": ticket.fingerprint, "reply_verbatim": reply, "accepted": ok,
                                    "why": why, "source": "supplied_answer"})
    if not ok:
        _release(mgr, rd.approval)
        record["status"] = "DECLINED"
        record["ticket_fingerprint"] = ticket.fingerprint
        journal.append("DECISION", t, record)
        return "DECLINED"
    result, pos = pm.enter(rd.approval, cand, cand.lots, t)
    journal.append("EXECUTION", t, paper_mod.result_to_journal(result))
    if pos is None:
        _release(mgr, rd.approval)
        record["status"] = "ENTRY_NOT_FILLED"
        journal.append("DECISION", t, record)
        return "ENTRY_NOT_FILLED"
    record["status"] = "ENTERED"
    record["position_id"] = pos.position_id
    journal.append("DECISION", t, record)
    return "ENTERED"


def _release(mgr, approval):
    try:
        mgr.cancel_unused(approval)
    except RiskViolation:
        pass                              # already released (for example the executor cancelled a stale approval)


def _snap(s):
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in s._asdict().items()}


def _journal_position(journal, t, res, pm, journaled_exit):
    pos, dec, ex = res.position, res.decision, res.execution
    if ex is None and (dec is None or dec.action == manager_mod.HOLD):
        return
    if ex is not None:
        journal.append("POSITION", t, {"position_id": pos.position_id, "state": pos.state, "action": dec.action,
                                       "primary_reason": dec.primary_reason, "triggers": list(dec.triggers),
                                       "forced": pos.forced, "exit_attempts": pos.exit_attempts,
                                       "pending_reasons": [c for c, _ in pos.last_pending_reasons],
                                       "execution": paper_mod.result_to_journal(ex)})
    if pos.state == manager_mod.CLOSED and pos.position_id not in journaled_exit:
        journaled_exit.add(pos.position_id)
        rep = pm.pnl_report(pos.position_id)
        journal.append("EXIT", t, {"position_id": pos.position_id, "exit_reason": pos.exit_reason,
                                    "forced": pos.forced, "gross_pnl_inr": rep["gross_pnl_inr"],
                                    "net_pnl_inr": rep["net_realized_pnl_inr"], "report": rep})
