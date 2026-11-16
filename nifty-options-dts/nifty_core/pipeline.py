"""The end-to-end paper pipeline for one trading day, replayed from a read-only provider or advanced live.

    data -> regime -> strategy -> risk -> TICKET -> human "yes" -> paper entry -> position management -> journal

The strategy step is one of two engines, chosen by the profile. The opening-range profile ("intraday") proposes a bull
call or bear put debit spread from one fixed setup. The adaptive profile ("adaptive") reads the market and lets the
selector pick one of four strategies (debit spread, credit spread, iron condor, iron butterfly) or no trade; its pick
must repeat for `entry_confirmations` consecutive slots before it goes to the risk gate. Everything after the strategy
step (risk gate, ticket, paper entry, position management, journal) is the same for every strategy.

Every tick is journaled where something was decided: NO TRADE and BLOCKED are journaled like trades. Without a
recorded "yes" for the exact ticket nothing is entered (AWAITING_APPROVAL). The replay applies the supplied answer at the
decision timestamp; the live session (nifty_core.live_session) keeps an unanswered ticket waiting for a short time and
applies the reply at the moment it arrives, because an approval goes stale after the staleness limit.

No part of this module places a real order: it only uses the paper executor and the read-only provider.
"""
import datetime
import hashlib
import json
import os
import re
from collections import Counter, namedtuple
from decimal import Decimal

from . import approval as approval_mod
from .contracts import ContractSpecs, hhmm
from .costs.engine import load_cost_engine
from .errors import ConfigError, JournalError, RiskViolation
from .execution import paper as paper_mod
from .guards import paper_only
from .intraday import params as intraday_params
from .journal import journal as journal_mod
from .market.calendar_nse import NseCalendar
from .market.ist import ist_datetime, slot_floor
from .positions import manager as manager_mod
from .positions import params as position_params
from .regime import params as regime_params
from .risk import engine as risk_engine
from .selector import engine as selector_engine
from .selector import exits as selector_exits
from .selector import params as selector_params_mod
from . import legs as legs_mod
from .risk import ledger as ledger_mod
from .strategy import engine as strategy_engine
from .strategy import params as strategy_params
from .versioned import ROOT

TICK_MINUTES = 5
Context = namedtuple("Context", (
    "cfg specs calendar cost_engine strategy_params regime_params position_params data_mode setup_params "
    "selector_params"), defaults=(None, None))
DaySummary = namedtuple("DaySummary", (
    "day status_counts entries exits criticals net_pnl_inr tickets_awaiting final_ledger journal_path"))
AUTO_REPLY = "(automatic paper entry: no human reply)"
AUTO_WHY = ("automatic paper entry is switched on for this run by the operator, so the risk-approved ticket is entered "
            "without a human reply. Paper only: the fill is simulated.")
PendingTicket = namedtuple("PendingTicket", "ticket approval candidate regime_label decided_at")


def profile_file(profile, name, required=True):
    """Path of a threshold file inside config/profiles/<profile>/, or None for the default (strict) files (and, when
    required=False, for a profile that simply has no such file)."""
    if profile in (None, "default"):
        return None
    if not re.match(r"^[a-z0-9_-]+$", profile):
        raise ConfigError("profile names use only lower-case letters, digits, - and _ (got %r)" % (profile,))
    path = os.path.join(ROOT, "config", "profiles", profile, name)
    if not os.path.exists(path):
        if not required:
            return None
        raise ConfigError("profile %r has no %s (looked in config/profiles/%s/)" % (profile, name, profile))
    return path


def load_context(cfg, data_mode="fixture", profile=None):
    """Everything the pipeline needs except the provider, built from the versioned files.

    `profile` picks the regime and strategy threshold files from config/profiles/<profile>/ (for example "human");
    None means the default files. Risk limits, costs, contracts and position rules are never part of a profile."""
    specs = ContractSpecs.load()
    setup_file = profile_file(profile, "setup_thresholds.json", required=False)      # only some profiles have one
    setup = intraday_params.load_params(setup_file) if setup_file else None
    selector_file = profile_file(profile, "selector_thresholds.json", required=False)     # only the adaptive profile
    selector = selector_params_mod.load_params(selector_file) if selector_file else None
    return Context(cfg, specs, NseCalendar.load(specs=specs, data_mode=data_mode), load_cost_engine(data_mode=data_mode),
                   strategy_params.load_params(profile_file(profile, "strategy_thresholds.json")),
                   regime_params.load_params(profile_file(profile, "regime_thresholds.json")),
                   position_params.load_params(), data_mode, setup, selector)


def _reason_list(reasons):
    return [{"code": r.code, "text": r.text} for r in reasons]


def _selector_inputs_hash(provider, sel, ctx):
    scenario = getattr(getattr(provider, "scenario", None), "name", "provider")
    cand = sel.candidate
    blob = {"scenario": scenario, "as_of": sel.as_of.isoformat(),
            "chain_snapshot": cand.chain_snapshot_time.isoformat() if cand is not None else None,
            "selected": sel.selected, "regime_fp": ctx.regime_params.fingerprint,
            "strategy_fp": ctx.strategy_params.fingerprint, "selector_fp": ctx.selector_params.fingerprint}
    return hashlib.sha256(json.dumps(blob, sort_keys=True).encode("utf-8")).hexdigest()


def _inputs_hash(provider, decision, ctx):
    scenario = getattr(getattr(provider, "scenario", None), "name", "provider")
    blob = {"scenario": scenario, "as_of": decision.decision_time.isoformat(),
            "candles_used": decision.regime.candles_used if decision.regime is not None else None,
            "chain_snapshot": decision.candidate.chain_snapshot_time.isoformat() if decision.candidate else None,
            "regime_fp": ctx.regime_params.fingerprint, "strategy_fp": ctx.strategy_params.fingerprint}
    if ctx.setup_params is not None:
        blob["setup_fp"] = ctx.setup_params.fingerprint
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


def _selector_summary(sel):
    """The selector's reading of the market and how each of the four strategies was judged, for the journal and the
    dashboard: every required condition with its pass / fail and detail."""
    a = sel.analysis
    return {"market": a.summary if a is not None else None, "selected": sel.selected, "confidence": sel.confidence,
            "selected_title": selector_engine.TITLES[sel.selected] if sel.selected else None,
            "evaluations": [{"strategy": selector_engine.TITLES[e.strategy], "eligible": e.eligible,
                             "confidence": e.confidence,
                             "conditions": [{"name": c.name, "passed": c.passed, "required": c.required, "detail": c.detail}
                                            for c in e.conditions]} for e in sel.evaluations]}


def _label(provider):
    """The provider's data label (FIXTURE_SYNTHETIC, RECORDED_REAL...), or None when it cannot say, which the engines
    treat as unverified in real mode."""
    return getattr(getattr(provider, "scenario", None), "data_label", None)


def _refuse_second_traded_run(journal, day):
    """Daily limits live inside one run, so a second run of a day that already traded would start with fresh limits.
    For REAL data that would be a way round the 2-trade and daily-loss limits, so it is refused. (A run that only
    showed tickets, or ended without an entry, may be repeated, which is how a ticket gets answered.)"""
    if journal.data_source != "real":
        return
    records = journal_mod.read_records(journal.path)
    runs_that_day = set(r["run_id"] for r in records if r["kind"] == "RUN_START" and r["payload"].get("day") == day.isoformat())
    for r in records:
        if r["kind"] == "DECISION" and r["run_id"] in runs_that_day and r["payload"].get("status") == "ENTERED":
            raise JournalError("DAY_ALREADY_TRADED", "%s already has a paper entry in this real-data journal; the "
                               "day's limits cannot be reset by running it again" % day.isoformat())


def restore_trades(journal, day):
    """The paper trades earlier runs of `day` made in this journal, oldest first. A trade whose run stopped before it
    was closed ("orphan") is valued at its last recorded mark, or at minus its entry costs if no mark was recorded."""
    records = journal_mod.read_records(journal.path)
    runs = set(r["run_id"] for r in records if r["kind"] == "RUN_START" and r["payload"].get("day") == day.isoformat())
    exits, marks = {}, {}
    for r in records:
        if r["run_id"] not in runs:
            continue
        if r["kind"] == "EXIT":
            exits[r["payload"]["position_id"]] = r
        elif r["kind"] == "MARK":
            marks[r["payload"]["position_id"]] = r
    out = []
    for r in records:
        p = r["payload"]
        if r["run_id"] not in runs or r["kind"] != "EXECUTION" or p.get("kind") != "ENTRY" or p.get("status") != "FILLED":
            continue
        pid = p["execution_id"]
        legs = [{"contract": l["contract"], "side": l["side"], "units": l["units"], "entry": l["fill_price"]}
                for l in p["legs"]]
        trade = {"position_id": pid, "approval_id": p["approval_id"], "candidate_id": p["candidate_id"],
                 "long": "+".join(sorted(l["contract"] for l in p["legs"] if l["side"] == "BUY")),
                 "short": "+".join(sorted(l["contract"] for l in p["legs"] if l["side"] == "SELL")),
                 "lots": p["legs"][0]["lots"],
                 "opened_at": p["executed_at"], "legs": legs}
        if pid in exits:
            x = exits[pid]
            trade.update(closed=True, administrative=bool(x["payload"].get("administrative")), closed_at=x["at"],
                         exit_reason=x["payload"].get("exit_reason"), gross=x["payload"]["gross_pnl_inr"],
                         net=x["payload"]["net_pnl_inr"])
        elif pid in marks:
            u = marks[pid]["payload"]["unrealized"]
            trade.update(closed=False, administrative=True, closed_at=marks[pid]["at"], exit_reason="RUNNER_STOPPED",
                         gross=u["gross_pnl_inr"], net=u["net_pnl_inr"], valued="last recorded mark")
        else:
            costs = p["actual"]["entry_costs"]["total_inr"]
            trade.update(closed=False, administrative=True, closed_at=r["at"], exit_reason="RUNNER_STOPPED",
                         gross="0.00", net="-" + costs, valued="entry costs only (no mark had been recorded)")
        out.append(trade)
    return out


class DayRunner(object):
    """One paper day, advanced one decision time at a time.

    `run_day` drives it over a finished day (replay). The live session drives it as wall-clock time passes. In replay a
    ticket is answered from the supplied `answers` at its own decision time. With hold_unanswered=True (live) an
    unanswered ticket keeps its reserved risk and waits in `pending` for `answer()`; it is released, never entered, when
    it goes stale or the next decision time arrives. Nothing is entered without an accepted reply for that exact ticket.
    """

    def __init__(self, provider, day, ctx, kill_dir, journal, answers=None, hold_unanswered=False, auto_entry=False,
                 restore=False):
        paper_only.assert_paper_only(ctx.cfg)
        self.provider, self.day, self.ctx, self.journal = provider, day, ctx, journal
        self.kill_dir = kill_dir
        self._pick, self._pick_count = None, 0        # adaptive: the strategy picked at the last slots, and for how many in a row
        self.answers = dict(answers or {})
        self.ticks = tick_times(ctx, day)
        if not self.ticks:
            raise ValueError("no trading session on %s" % day)
        # A fresh run of a day that already traded would start with fresh limits, so it is refused (replays, run-day).
        # A live session that is RESTARTED instead restores the day from the journal: earlier trades are carried into
        # the ledger, so the day's trade count and loss limits carry on and cannot be reset.
        self._carried = restore_trades(journal, day) if restore else []
        self.carry_notes = []
        if not restore:
            _refuse_second_traded_run(journal, day)
        cfg = ctx.cfg
        self.mgr = risk_engine.RiskManager(cfg, ctx.cost_engine, ctx.specs, ctx.calendar, kill_dir, day)
        self.ex = paper_mod.PaperExecutor(cfg, ctx.cost_engine, ctx.specs, ctx.calendar, self.mgr, provider)
        setup_exit = None
        if ctx.selector_params is not None:
            def setup_exit(now, entry_label, candidate=None):
                if candidate is None:
                    return None
                return selector_exits.early_exit_at(provider, ctx.calendar, ctx.regime_params, ctx.selector_params, now,
                                                    candidate)
        elif ctx.setup_params is not None:
            def setup_exit(now, entry_label, candidate=None):
                return strategy_engine.setup_has_failed(provider, ctx.calendar, ctx.regime_params, ctx.setup_params,
                                                        now, entry_label)
        self.pm = manager_mod.PositionManager(cfg, ctx.position_params, self.ex, self.mgr, provider, ctx.calendar,
                                              ctx.regime_params, ctx.cost_engine, data_mode=ctx.data_mode,
                                              data_label=_label(provider), setup_exit_fn=setup_exit)
        self.auto_entry = bool(auto_entry)
        self.status_counts, self.awaiting, self.entries, self.locked = Counter(), [], 0, False
        self.journaled_exit = set()
        self.pending = {} if hold_unanswered else None
        self.last_decision = None

    def start(self, at):
        ctx = self.ctx
        payload = {
            "notice": "PAPER trading only, SIMULATED fills. Not financial advice.", "day": self.day.isoformat(),
            "data_mode": ctx.data_mode, "config_fingerprint": risk_engine.config_fingerprint(ctx.cfg),
            "strategy_thresholds": ctx.strategy_params.fingerprint, "regime_thresholds": ctx.regime_params.fingerprint,
            "position_thresholds": ctx.position_params.fingerprint, "answers_supplied": len(self.answers),
            "scenario": getattr(getattr(self.provider, "scenario", None), "name", None)}
        if ctx.setup_params is not None:
            payload["intraday_setup_thresholds"] = ctx.setup_params.fingerprint
        if ctx.selector_params is not None:
            payload["selector_thresholds"] = ctx.selector_params.fingerprint
        if self._carried:
            payload["restored_trades"] = [{"position_id": t["position_id"], "closed_in_earlier_run": t["closed"]}
                                          for t in self._carried]
        self.journal.append("RUN_START", at, payload)
        self._apply_carry(at)

    def _apply_carry(self, at):
        """Bring the day's earlier trades into this run. A position an earlier run left open can no longer be managed, so
        it is closed ADMINISTRATIVELY at its last recorded mark (journaled as an EXIT with exit_reason RUNNER_STOPPED and
        administrative true), then every earlier trade is added to the risk ledger as closed."""
        if not self._carried:
            return
        positions = []
        for t in self._carried:
            if not t["closed"]:
                self.journal.append("EXIT", at, {
                    "position_id": t["position_id"], "exit_reason": "RUNNER_STOPPED", "forced": False,
                    "gross_pnl_inr": t["gross"], "net_pnl_inr": t["net"], "administrative": True,
                    "note": ("the run that opened this paper position stopped while it was open, so it could not be "
                             "managed. Closed administratively, valued at its %s. Not a simulated exit fill." % t["valued"])})
                self.carry_notes.append("restored: position %s was left open by an earlier run and is closed "
                                        "administratively at its %s (net INR %s)" % (t["position_id"][:10], t["valued"], t["net"]))
            positions.append(ledger_mod.Position(
                t["approval_id"], t["candidate_id"], "RESTORED", t["long"], t["short"], t["lots"], Decimal(0), Decimal(0),
                datetime.datetime.fromisoformat(t["opened_at"]), ledger_mod.CLOSED, Decimal(t["net"]),
                datetime.datetime.fromisoformat(at.isoformat() if not t["closed"] else t["closed_at"])))
        self.mgr.carry_over(positions)
        self.carry_notes.append("restored %d earlier trade(s) of today from this journal: %d counted against today's limit "
                                "of %d" % (len(self._carried), len(self._carried), self.ctx.cfg.max_trades_per_day))

    def tick(self, t, skip_entry_reason=None):
        """Manage open positions, then take at most one entry decision, as of time t. Returns the decision status.

        `skip_entry_reason` (live only): the slot's candle never became final, so no NEW entry is decided on it. The
        reason is journaled and the status is SLOT_SKIPPED; open positions were already managed above."""
        cfg, journal = self.ctx.cfg, self.journal
        self.expire_pending(t, everything=True)
        # 1 manage open positions first
        for res in self.pm.manage_all(t):
            _journal_position(journal, t, res, self.pm, self.journaled_exit)
        snap = self.mgr.snapshot()
        if slot_floor(t).time() > hhmm(cfg.entry_window_end_ist) or t.time() >= hhmm(cfg.flat_by_ist):
            return None
        if self.pm.active():
            return None
        if snap.trades_today >= cfg.max_trades_per_day or snap.realized_loss_used_inr >= cfg.max_daily_loss_inr:
            if not self.locked:
                self.locked = True
                journal.append("DAY_LOCKED", t, {"reason": "trade count or daily loss limit reached",
                                                  "trades_today": snap.trades_today,
                                                  "realized_loss_used_inr": str(snap.realized_loss_used_inr)})
            return None
        if skip_entry_reason is not None:
            self._pick, self._pick_count = None, 0                  # a slot without a verdict breaks the confirmation run
            journal.append("DECISION", t, {"decision_time": t.isoformat(), "status": "SLOT_SKIPPED",
                                            "reason": skip_entry_reason, "regime": {"label": "NOT_EVALUATED"}})
            self.status_counts["SLOT_SKIPPED"] += 1
            return "SLOT_SKIPPED"
        # 2 one decision
        status = self._decide(t)
        self.status_counts[status] += 1
        if status == "ENTERED":
            self.entries += 1
        return status

    def finish(self, at):
        journal, pm = self.journal, self.pm
        self.expire_pending(at, everything=True)
        pm.end_of_session(at)
        for exc in pm.critical_exceptions:
            journal.append("CRITICAL", at, {"position_id": exc.position_id, "text": exc.text})
        closed = [p for p in pm.positions() if p.state == manager_mod.CLOSED]
        exits = len(closed)
        net = sum((Decimal(pm.pnl_report(p.position_id)["net_realized_pnl_inr"]) for p in closed), Decimal(0))
        snap = self.mgr.snapshot()
        summary = DaySummary(self.day, dict(sorted(self.status_counts.items())), self.entries, exits,
                             len(pm.critical_exceptions), net, tuple(self.awaiting), snap, journal.path)
        journal.append("RUN_END", at, {
            "status_counts": summary.status_counts, "entries": self.entries, "completed_trades": exits,
            "critical_exceptions": summary.criticals, "net_pnl_inr": str(net), "tickets_awaiting": list(self.awaiting),
            "ledger": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in snap._asdict().items()},
            "caveat": "Simulation on synthetic or model-priced data. This is not evidence of profitability."
            if journal.data_source == "fixture" else "Paper results on recorded data."})
        return summary

    # ---- live only: tickets that wait for a reply
    def expire_pending(self, now, everything=False):
        """Release the reserved risk of unanswered tickets that are stale (all of them when everything=True)."""
        if not self.pending:
            return
        limit = self.ctx.cfg.data_stale_after_seconds
        for fp, p in list(self.pending.items()):
            if everything or (now - p.decided_at).total_seconds() > limit:
                del self.pending[fp]
                _release(self.mgr, p.approval)
                self._resolve_last(fp, "TICKET_EXPIRED")
                self.journal.append("DECISION", now, {
                    "decision_time": p.decided_at.isoformat(), "status": "TICKET_EXPIRED", "ticket_fingerprint": fp,
                    "follow_up": True, "regime": {"label": p.regime_label},
                    "reason": "no accepted reply before the ticket went stale or the next decision time arrived"})

    def _resolve_last(self, fingerprint, status):
        """Keep the in-memory 'latest decision' (shown on the dashboard) in step with what became of its ticket."""
        d = self.last_decision
        if d is not None and d.get("ticket_fingerprint") == fingerprint:
            d["status"] = status

    def answer(self, fingerprint, reply, now):
        """Apply a reply to a waiting ticket at `now`. Returns the resulting status (or UNKNOWN_TICKET)."""
        p = (self.pending or {}).get(fingerprint)
        if p is None:
            self.journal.append("APPROVAL", now, {
                "fingerprint": fingerprint, "reply_verbatim": reply, "accepted": False,
                "why": "no ticket with this fingerprint is waiting (it may have expired or been answered)",
                "source": "live_answer"})
            return "UNKNOWN_TICKET"
        del self.pending[fingerprint]
        ok, why = approval_mod.parse_yes(reply, p.ticket)
        self.journal.append("APPROVAL", now, {"fingerprint": fingerprint, "reply_verbatim": reply, "accepted": ok,
                                               "why": why, "source": "live_answer"})
        follow = {"decision_time": p.decided_at.isoformat(), "ticket_fingerprint": fingerprint, "follow_up": True,
                  "regime": {"label": p.regime_label}}
        if not ok:
            _release(self.mgr, p.approval)
            self._resolve_last(fingerprint, "DECLINED")
            follow["status"] = "DECLINED"
            self.journal.append("DECISION", now, follow)
            return "DECLINED"
        result, pos = self.pm.enter(p.approval, p.candidate, p.candidate.lots, now)
        self.journal.append("EXECUTION", now, paper_mod.result_to_journal(result))
        if pos is None:
            _release(self.mgr, p.approval)
            self._resolve_last(fingerprint, "ENTRY_NOT_FILLED")
            follow["status"] = "ENTRY_NOT_FILLED"
            follow["reasons"] = _reason_list(result.reasons)
            self.journal.append("DECISION", now, follow)
            return "ENTRY_NOT_FILLED"
        self._resolve_last(fingerprint, "ENTERED")
        follow["status"], follow["position_id"] = "ENTERED", pos.position_id
        self.journal.append("DECISION", now, follow)
        self.entries += 1
        self.status_counts["ENTERED"] += 1
        return "ENTERED"

    def _decide(self, t):
        if self.ctx.selector_params is not None:
            return self._decide_adaptive(t)
        ctx, mgr, pm, journal, provider = self.ctx, self.mgr, self.pm, self.journal, self.provider
        d = strategy_engine.decide_at(provider, ctx.calendar, ctx.specs, ctx.cfg, ctx.strategy_params,
                                      ctx.regime_params, t, ctx.data_mode, _label(provider), ctx.setup_params)
        record = {"decision_time": t.isoformat(), "inputs_hash": _inputs_hash(provider, d, ctx),
                  "regime": {"label": d.regime.label, "evidence_score": None if d.regime.evidence_score is None
                             else str(d.regime.evidence_score), "bull_votes": d.regime.bull_votes,
                             "bear_votes": d.regime.bear_votes, "reasons": _reason_list(d.regime.reasons),
                             "thresholds_fingerprint": d.regime.thresholds_fingerprint},
                  "strategy": {"outcome": d.outcome, "reasons": _reason_list(d.reasons),
                               "candidates_tested": len(d.considered),
                               "thresholds_fingerprint": d.thresholds_fingerprint}}
        if d.regime is not None and d.regime.metrics.get("setup"):
            record["setup"] = d.regime.metrics["setup"]       # the opening-range rules, one by one (value, limit, passed)
        self.last_decision = record
        if d.outcome != strategy_engine.CANDIDATE:
            record["status"] = "NO_TRADE"
            journal.append("DECISION", t, record)
            return "NO_TRADE"
        offered = (d.candidate,) + tuple(d.alternates)
        return self._risk_and_enter(offered, record, t, d.regime.label)

    def _decide_adaptive(self, t):
        """The selector reads the market and picks one of four strategies, or none. A pick is only offered to the risk gate
        once it has been the pick at `entry_confirmations` consecutive slots, so a one-slot flicker never trades."""
        ctx, journal, provider = self.ctx, self.journal, self.provider
        p = ctx.selector_params
        sel = selector_engine.select_at(provider, ctx, p, t, False, self.kill_dir, _label(provider))
        analysis = sel.analysis
        record = {"decision_time": t.isoformat(), "inputs_hash": _selector_inputs_hash(provider, sel, ctx),
                  "regime": {"label": analysis.structure if analysis is not None else (sel.regime_label or "UNKNOWN"),
                             "evidence_score": None, "bull_votes": None, "bear_votes": None, "reasons": [],
                             "thresholds_fingerprint": ctx.regime_params.fingerprint},
                  "strategy": {"outcome": "CANDIDATE" if sel.outcome == selector_engine.TRADE else "NO_TRADE",
                               "reasons": _reason_list(sel.reasons), "candidates_tested": len(selector_engine.STRATEGIES),
                               "thresholds_fingerprint": p.fingerprint},
                  "selector": _selector_summary(sel)}
        self.last_decision = record
        if sel.outcome != selector_engine.TRADE:
            self._pick, self._pick_count = None, 0
            record["status"] = "NO_TRADE"
            journal.append("DECISION", t, record)
            return "NO_TRADE"
        cand = sel.candidate
        kind = cand.strategy_type
        self._pick_count = self._pick_count + 1 if kind == self._pick else 1
        self._pick = kind
        if self._pick_count < p.entry_confirmations:
            record["strategy"]["outcome"] = "NO_TRADE"
            record["strategy"]["reasons"] = [{"code": "AWAITING_CONFIRMATION", "text": (
                "%s is the pick (confidence %d) but needs the same pick at %d consecutive slots before entry (%d so far): "
                "a one-slot flicker is not traded" % (selector_engine.KIND_TITLES[kind], sel.confidence,
                                                      p.entry_confirmations, self._pick_count))}]
            record["selector"]["awaiting_confirmation"] = {"strategy": kind, "have": self._pick_count,
                                                           "need": p.entry_confirmations}
            record["status"] = "NO_TRADE"
            journal.append("DECISION", t, record)
            return "NO_TRADE"
        status = self._risk_and_enter((cand,), record, t, cand.regime_at_entry["label"])
        if status == "ENTERED":
            self._pick, self._pick_count = None, 0
        return status

    def _risk_and_enter(self, offered, record, t, regime_label):
        """Offer the candidates to the risk gate in rank order and take the first one that fits. If none fits, the
        best-ranked one is the one reported as blocked. Blocked offers reserve nothing. Then ticket and entry."""
        mgr, journal = self.mgr, self.journal
        cand, rd, blocked_first = None, None, []
        for option in offered:
            attempt = mgr.request_entry(option, t)
            if attempt.outcome == risk_engine.APPROVED:
                cand, rd = option, attempt
                break
            long_symbol, short_symbol = legs_mod.symbol_fields(option)
            blocked_first.append({"long": long_symbol, "short": short_symbol,
                                  "reason_codes": [r.code for r in attempt.reasons]})
            if rd is None:
                cand, rd = option, attempt
        record["strategy"]["candidate"] = strategy_engine.candidate_to_journal(cand)
        if len(offered) > 1:
            record["strategy"]["offered_to_risk_gate"] = len(offered)
            if rd.outcome == risk_engine.APPROVED and blocked_first:
                record["strategy"]["higher_ranked_blocked_by_risk"] = blocked_first
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
        reply = self.answers.get(ticket.fingerprint)
        if reply is None and self.auto_entry:
            # Operator-chosen automatic PAPER entry (live_run --entry-mode auto). Every gate above still applied: the
            # strategy, the risk limits, the kill switch and the executor's own checks. It is journaled as such.
            journal.append("APPROVAL", t, {"fingerprint": ticket.fingerprint, "reply_verbatim": AUTO_REPLY,
                                            "accepted": True, "why": AUTO_WHY, "source": "auto_paper_entry"})
            return self._enter(rd, cand, record, t)
        if reply is None:
            self.awaiting.append(ticket.fingerprint)
            if self.pending is None:
                _release(mgr, rd.approval)
            else:                       # live: keep the reservation until a reply, expiry or the next decision time
                self.pending[ticket.fingerprint] = PendingTicket(ticket, rd.approval, cand, regime_label, t)
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
        return self._enter(rd, cand, record, t)

    def _enter(self, rd, cand, record, t):
        """Paper entry of an approved candidate (a human yes, a supplied answer, or the automatic paper policy)."""
        result, pos = self.pm.enter(rd.approval, cand, cand.lots, t)
        self.journal.append("EXECUTION", t, paper_mod.result_to_journal(result))
        if pos is None:
            _release(self.mgr, rd.approval)
            record["status"] = "ENTRY_NOT_FILLED"
            record["reasons_not_filled"] = _reason_list(result.reasons)
            self.journal.append("DECISION", t, record)
            return "ENTRY_NOT_FILLED"
        record["status"] = "ENTERED"
        record["position_id"] = pos.position_id
        self.journal.append("DECISION", t, record)
        return "ENTERED"


def run_day(provider, day, ctx, kill_dir, journal, answers=None):
    """Replay one trading day. `answers` maps a ticket fingerprint to the human's reply, word for word.

    Returns a DaySummary. Nothing is entered without an accepted reply for that exact ticket.
    """
    runner = DayRunner(provider, day, ctx, kill_dir, journal, answers)
    runner.start(runner.ticks[0])
    for t in runner.ticks:
        runner.tick(t)
    return runner.finish(runner.ticks[-1])


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
