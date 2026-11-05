"""A live PAPER session: the same pipeline as a replay, advanced as the clock moves, with a running mark-to-market.

    LiveSession(ctx, kill_dir, journal, day).step(now, scenario)   # every poll: manage/decide when a slot is due, mark
    LiveSession.answer(fingerprint, reply, now)                    # a human reply to one waiting ticket
    LiveSession.snapshot(now)                                      # JSON-safe view for the local dashboard

* No network here. The caller (nifty_live.live_run) records quotes to files and hands over a validated Scenario that
  holds everything recorded so far; the session reads it through the same read-only provider interface as a replay.
* Decisions and position management happen once per five-minute slot, exactly as in a replay, at the real time the slot
  is processed. Between slots only the display is refreshed (nothing is decided and nothing is closed).
* An entry still needs an accepted "yes" for that exact ticket, applied while the approval is fresh
  (cfg.data_stale_after_seconds). A ticket nobody answers is released, never entered.
* UNREALISED P&L is a simulated mark to liquidation (long leg at bid minus slippage, short leg at ask plus slippage, net
  of the actual entry costs and estimated exit costs). It is a display figure and is never a performance claim.
PAPER trading only. Not financial advice.
"""
import datetime
from collections import deque
from decimal import Decimal

from . import pipeline
from .costs.models import paise
from .data.fixture_provider import FixtureProvider
from .data.provider import MarketDataProvider
from .errors import DataNotAvailable, NiftyError
from .guards import paper_only
from .intraday import setup as intraday_setup
from .market.ist import ist_datetime, to_ist
from .positions import manager as manager_mod

UNREALISED_NOTE = ("UNREALISED: a simulated mark to liquidation on live quotes, after entry costs and estimated exit "
                   "costs. It moves with every quote and is not a result and not evidence of profitability.")
MAX_CURVE_POINTS = 4000
MAX_EVENTS = 60
MAX_SPOT_POINTS = 6000
CHAIN_STRIKES_EACH_SIDE = 5


class _RollingProvider(MarketDataProvider):
    """Forwards every read to whichever provider the session holds right now (the newest recorded data)."""

    def __init__(self, current):
        self._current = current

    @property
    def scenario(self):
        return self._current().scenario

    def get_clock(self):
        return self._current().get_clock()

    def get_underlying_bars(self, interval_minutes, start, end, as_of=None):
        return self._current().get_underlying_bars(interval_minutes, start, end, as_of)

    def get_option_chain(self, expiry=None, as_of=None):
        return self._current().get_option_chain(expiry, as_of)

    def get_quote(self, contract, as_of=None):
        return self._current().get_quote(contract, as_of)

    def get_expiries(self, as_of=None):
        return self._current().get_expiries(as_of)

    def get_event_calendar(self, day):
        return self._current().get_event_calendar(day)


def _s(x):
    return None if x is None else str(x)


class LiveSession(object):
    def __init__(self, ctx, kill_dir, journal, day, run_label=None, auto_entry=False):
        paper_only.assert_paper_only(ctx.cfg)
        self.auto_entry = bool(auto_entry)           # operator's choice: enter risk-approved PAPER tickets automatically
        self.ctx, self.kill_dir, self.journal, self.day = ctx, kill_dir, journal, day
        self.run_label = run_label
        self._inner = None
        self._provider = _RollingProvider(lambda: self._inner)
        self._slots = pipeline.tick_times(ctx, day)
        if not self._slots:
            raise ValueError("no trading session on %s" % day)
        self._last_slot = None
        self.runner = None
        self.finished = False
        self.summary = None
        self._curve = []
        self._events = deque(maxlen=MAX_EVENTS)
        self._exits_seen = 0
        self._spots = []
        self._open_notes = []
        self._last_now = None

    @property
    def slots(self):
        return tuple(self._slots)

    # ---- events shown on the dashboard
    def note(self, now, text):
        self._events.append({"at": to_ist(now).isoformat(), "text": text})

    # ---- stepping
    def _open(self, now):
        self.runner = pipeline.DayRunner(self._provider, self.day, self.ctx, self.kill_dir, self.journal,
                                         hold_unanswered=not self.auto_entry, auto_entry=self.auto_entry,
                                         restore=True)
        self.runner.start(now)
        self._open_notes = list(self.runner.carry_notes)
        self.note(now, "paper session started (real quotes, simulated fills, %s)" % (
            "AUTOMATIC paper entry and exit" if self.auto_entry else "entries need your yes"))

    def step(self, now, scenario):
        """Advance to `now` using everything recorded so far. Returns a list of short event strings."""
        if self.finished:
            return []
        now = to_ist(now)
        self._inner = FixtureProvider(scenario)
        out = []
        if self.runner is None:
            self._open(now)
            out.extend(self._open_notes)
        self.runner.expire_pending(now)
        due = [s for s in self._slots if s <= now]
        if due and due[-1] != self._last_slot:
            self._last_slot = due[-1]
            status = self.runner.tick(now)
            if status is not None:
                regime = (self.runner.last_decision or {}).get("regime", {}).get("label")
                out.append("decision %s (regime %s)" % (status, regime))
            else:
                out.append("slot %s: positions managed, no new decision" % due[-1].strftime("%H:%M"))
            self._journal_marks(now)
            self._note_exits(now, out)
        for text in out:
            self.note(now, text)
        self._last_now = now
        self._record_spot(now)
        self._append_curve(now)
        return out

    def _record_spot(self, now):
        """Remember each new snapshot's spot, so the dashboard can draw the day's price as it moves."""
        try:
            chain = self._inner.get_option_chain(as_of=now)
        except DataNotAvailable:
            return
        stamp = chain.snapshot_time.isoformat()
        if self._spots and self._spots[-1][0] == stamp:
            return
        self._spots.append([stamp, str(chain.spot)])
        if len(self._spots) > MAX_SPOT_POINTS:
            del self._spots[: len(self._spots) - MAX_SPOT_POINTS]

    def _bars(self, now):
        if self._inner is None:
            return ()
        session = self.ctx.calendar.session(now.date())
        if session is None:
            return ()
        try:
            return self._inner.get_underlying_bars(self.ctx.regime_params.interval_minutes,
                                                   ist_datetime(session.date, session.open),
                                                   ist_datetime(session.date, session.close), as_of=now)
        except DataNotAvailable:
            return ()

    def _levels(self, bars, now):
        if self.ctx.setup_params is None:
            return None
        lv = intraday_setup.levels(bars, now, self.ctx.setup_params)
        return None if lv is None else {k: str(paise(v)) for k, v in lv.items()}

    def _chain_view(self, chain):
        """Strikes around the money for the expiry that matters (the held position's, else the nearest), with the legs
        of any open paper position marked."""
        held, held_expiry = {}, None
        if self.runner is not None:
            for p in self.runner.pm.active():
                for f in self.runner.ex.position(p.position_id).entry_fills:
                    held[(f.contract.strike, f.contract.option_type)] = f.side
                    held_expiry = f.contract.expiry
        expiries = chain.expiries()
        expiry = held_expiry or (expiries[0] if expiries else None)
        if expiry is None:
            return None
        by_strike = {}
        for q in chain.for_expiry(expiry):
            by_strike.setdefault(q.contract.strike, {})[q.contract.option_type] = q
        strikes = sorted(by_strike)
        atm = min(strikes, key=lambda k: abs(k - chain.spot))
        i = strikes.index(atm)

        def side(q):
            return None if q is None else {"bid": _s(q.bid), "ask": _s(q.ask), "ltp": _s(q.ltp),
                                           "oi": q.open_interest, "volume": q.volume}
        rows = [{"strike": str(k), "atm": k == atm, "ce": side(by_strike[k].get("CE")), "pe": side(by_strike[k].get("PE")),
                 "held": {"CE": held.get((k, "CE")), "PE": held.get((k, "PE"))}}
                for k in strikes[max(0, i - CHAIN_STRIKES_EACH_SIDE): i + CHAIN_STRIKES_EACH_SIDE + 1]]
        return {"expiry": expiry.isoformat(), "rows": rows}

    def answer(self, fingerprint, reply, now):
        """A reply to one waiting ticket, applied at `now`. Returns the resulting status."""
        now = to_ist(now)
        if self.runner is None or self.finished:
            return "NO_SESSION"
        status = self.runner.answer(fingerprint, reply, now)
        self.note(now, "reply for ticket %s: %s" % (fingerprint, status))
        return status

    def finish(self, now):
        if self.finished or self.runner is None:
            self.finished = True
            return self.summary
        now = to_ist(now)
        self.summary = self.runner.finish(now)
        self.finished = True
        self.note(now, "session finished: %d completed paper trade(s), %d critical exception(s)" % (
            self.summary.exits, self.summary.criticals))
        return self.summary

    # ---- marks
    def _note_exits(self, now, out):
        done = len(self.runner.journaled_exit)
        if done > self._exits_seen:
            self._exits_seen = done
            out.append("a paper position was closed")

    def _unrealised(self, position_id, now):
        """(unrealised dict or None, exit fills, [problem texts]) for one open position. Never raises."""
        pm = self.runner.pm
        try:
            fills, problems = self.runner.ex.preview_exit(position_id, now)
        except NiftyError as e:
            return None, (), [str(e)]
        if problems:
            return None, (), ["%s: %s" % (p.code, p.text) for p in problems]
        try:
            rep = pm.pnl_report(position_id, now)
        except NiftyError as e:
            return None, fills, [str(e)]
        return rep.get("unrealized"), fills, []

    def _journal_marks(self, now):
        for p in self.runner.pm.active():
            u, _, _ = self._unrealised(p.position_id, now)
            if u is not None:
                self.journal.append("MARK", now, {"position_id": p.position_id, "state": p.state,
                                                  "unrealized": u, "note": UNREALISED_NOTE})

    def _totals(self, now):
        """(realised net, unrealised net or None-if-any-mark-missing, per-position views)."""
        pm, ex, params = self.runner.pm, self.runner.ex, self.ctx.position_params
        realised, unrealised, open_views, closed_views, marked_all = Decimal(0), Decimal(0), [], [], True
        for p in pm.positions():
            epos = ex.position(p.position_id)
            if p.state == manager_mod.CLOSED:
                rep = pm.pnl_report(p.position_id)
                net = Decimal(rep["net_realized_pnl_inr"])
                realised += net
                closed_views.append({
                    "position_id": p.position_id, "strategy": p.strategy_type, "entry_time": p.entry_time.isoformat(),
                    "closed_at": p.closed_at, "exit_reason": p.exit_reason, "gross_pnl_inr": rep["gross_pnl_inr"],
                    "net_pnl_inr": rep["net_realized_pnl_inr"],
                    "legs": [{"contract": f.contract.symbol, "side": f.side, "units": f.units,
                              "entry": str(f.fill_price)} for f in epos.entry_fills]})
                continue
            defined = epos.approval.defined_risk_inr
            u, fills, problems = self._unrealised(p.position_id, now)
            exit_by_leg = {f.leg_id: f for f in fills}
            legs = []
            for f in epos.entry_fills:
                x = exit_by_leg.get(f.leg_id)
                leg = {"leg": f.leg_id, "contract": f.contract.symbol, "side": f.side, "units": f.units,
                       "entry": str(f.fill_price), "mark": None, "bid": None, "ask": None, "leg_gross_inr": None}
                if x is not None:
                    leg.update(mark=str(x.fill_price), bid=str(x.bid), ask=str(x.ask))
                    sign = 1 if f.side == "BUY" else -1
                    leg["leg_gross_inr"] = str(paise(sign * (x.fill_price - f.fill_price) * f.units))
                legs.append(leg)
            if u is not None:
                unrealised += Decimal(u["net_pnl_inr"])
            else:
                marked_all = False
            open_views.append({
                "position_id": p.position_id, "state": p.state, "strategy": p.strategy_type,
                "entry_time": p.entry_time.isoformat(), "lots": p.lots, "legs": legs, "unrealized": u,
                "mark_problems": problems, "defined_risk_inr": str(defined),
                "stop_at_net_inr": str(paise(-(defined * params.stop_loss_pct_of_defined_risk / 100))),
                "target_at_net_inr": str(paise(defined * params.profit_target_pct_of_defined_risk / 100)),
                "exit_reason": p.exit_reason, "exit_attempts": p.exit_attempts,
                "pending_reasons": [c for c, _ in p.last_pending_reasons], "critical": p.critical})
        for t in self.runner._carried:                      # trades of earlier runs today, valued as they were closed
            realised += Decimal(t["net"])
            closed_views.insert(0, {
                "position_id": t["position_id"], "strategy": "RESTORED", "entry_time": t["opened_at"],
                "closed_at": t["closed_at"], "exit_reason": t["exit_reason"], "gross_pnl_inr": t["gross"],
                "net_pnl_inr": t["net"], "administrative": not t["closed"] or t["administrative"], "legs": t["legs"]})
        return realised, (unrealised if marked_all else None), open_views, closed_views

    def _append_curve(self, now):
        if self.runner is None:
            return
        realised, unrealised, _, _ = self._totals(now)
        total = realised + (unrealised if unrealised is not None else Decimal(0))
        self._curve.append([now.isoformat(), str(paise(total))])
        if len(self._curve) > MAX_CURVE_POINTS:
            del self._curve[: len(self._curve) - MAX_CURVE_POINTS]

    # ---- the dashboard view
    def snapshot(self, now):
        now = to_ist(now)
        cfg = self.ctx.cfg
        view = {
            "kind": "LIVE_PAPER_STATE", "paper": True, "updated_at": now.isoformat(), "day": self.day.isoformat(),
            "data_mode": self.ctx.data_mode, "label": "PAPER - live quotes, SIMULATED fills", "note": UNREALISED_NOTE,
            "entry_mode": "AUTO" if self.auto_entry else "MANUAL",
            "journal_name": journal_mod_name(self.journal.path), "run_id": self.journal.run_id,
            "status": "NOT_STARTED", "kill_switch": paper_only.kill_switch_active(self.kill_dir),
            "approval_window_seconds": cfg.data_stale_after_seconds,
            "slots": {"first": self._slots[0].isoformat(), "last": self._slots[-1].isoformat(),
                      "next": next((s.isoformat() for s in self._slots if s > now), None)},
            "limits": {"max_trades_per_day": cfg.max_trades_per_day, "risk_per_trade_inr": _s(cfg.risk_per_trade_inr),
                       "max_daily_loss_inr": _s(cfg.max_daily_loss_inr), "flat_by_ist": cfg.flat_by_ist,
                       "entry_window": [cfg.entry_window_start_ist, cfg.entry_window_end_ist]},
            "market": None, "last_decision": None, "pending_tickets": [], "open_positions": [], "closed_trades": [],
            "totals": None, "curve": self._curve, "events": list(self._events), "spot_series": [], "candle_closes": [],
            "levels": None, "chain_view": None}
        chain = None
        if self._inner is not None:
            try:
                chain = self._inner.get_option_chain(as_of=now)         # only what was known at `now`
            except DataNotAvailable:
                chain = None
        if chain is not None:
            bars = self._bars(now)
            view["spot_series"] = self._spots
            view["candle_closes"] = [[c.end.isoformat(), str(c.close)] for c in bars]
            view["levels"] = self._levels(bars, now)
            view["chain_view"] = self._chain_view(chain)
            view["market"] = {"spot": _s(chain.spot), "chain_time": chain.snapshot_time.isoformat(),
                              "data_age_seconds": int((now - chain.snapshot_time).total_seconds()),
                              "quotes_in_snapshot": len(chain.quotes), "data_label": self._inner.scenario.data_label}
        if self.runner is None:
            return view
        view["status"] = "FINISHED" if self.finished else (
            "WAITING_FOR_FIRST_SLOT" if self._last_slot is None else "RUNNING")
        realised, unrealised, open_views, closed_views = self._totals(now)
        view["open_positions"], view["closed_trades"] = open_views, closed_views
        view["totals"] = {
            "realised_net_inr": str(paise(realised)),
            "unrealised_net_inr": None if unrealised is None else str(paise(unrealised)),
            "day_total_inr": None if unrealised is None else str(paise(realised + unrealised)),
            "trades_today": self.runner.entries + len(self.runner._carried), "completed_trades": len(closed_views)}
        d = self.runner.last_decision
        if d is not None:
            why = (d.get("risk", {}).get("reasons") if d.get("status") == "BLOCKED" else
                   d.get("strategy", {}).get("reasons")) or d.get("regime", {}).get("reasons") or []
            view["last_decision"] = {"decision_time": d["decision_time"], "status": d.get("status"),
                                     "regime": d["regime"]["label"], "strategy_outcome": d["strategy"]["outcome"],
                                     "reasons": [r["text"] for r in why][:4]}
        for fp, p in (self.runner.pending or {}).items():
            view["pending_tickets"].append({
                "fingerprint": fp, "text": p.ticket.text, "decided_at": p.decided_at.isoformat(),
                "expires_at": (p.decided_at + datetime.timedelta(seconds=cfg.data_stale_after_seconds)).isoformat()})
        return view


def journal_mod_name(path):
    return path.replace("\\", "/").rsplit("/", 1)[-1]
