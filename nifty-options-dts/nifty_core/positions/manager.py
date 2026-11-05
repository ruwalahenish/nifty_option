"""Version 1 position manager: HOLD or CLOSE. Nothing else.

There is no way here to add lots, average, roll a strike, add a leg or run a rescue trade: the manager has no such
operation, and a position can only be closed as the same two contracts and lots it was opened with.

STATES
    OPEN                filled entry, being held
    EXIT_PENDING        a close has been decided; the exit could not be filled yet (missing, stale or one-sided
                        quotes, or too little quoted size). It keeps retrying with fresh data. If the forced flat-by
                        time has been reached the position is marked forced.
    CLOSED              both legs exited by simulated fills; realized P&L recorded
    UNCLOSED_CRITICAL   the session ended while the position was still open. This is a critical exception and is
                        NEVER shown as closed; the risk ledger keeps it open.
    OPEN -> EXIT_PENDING | CLOSED | UNCLOSED_CRITICAL;  EXIT_PENDING -> CLOSED | UNCLOSED_CRITICAL.

EXIT REASONS (priority order when several apply; all that apply are recorded)
    FORCED_FLAT_BY      now >= flat_by_ist. An exit attempt is required on every call until the session closes.
    MANUAL_PAPER_EXIT   explicitly requested
    DATA_SAFETY         exit quotes or the regime data were unusable on data_safety_failed_checks consecutive checks
    STOP_LOSS           unrealized net P&L <= -(stop_loss_pct_of_defined_risk % of the approved defined risk)
    REGIME_INVALIDATION the regime turned to the opposite direction (or HIGH_VOLATILITY), when enabled
    SETUP_INVALIDATED   (only when a setup check is supplied) the entry setup failed, for example price closed back
                        inside the opening range it had broken out of
    PROFIT_TARGET       unrealized net P&L >= profit_target_pct_of_defined_risk % of the approved defined risk
    Once a close is decided it is sticky: the manager keeps trying even if the trigger later fades.

UNREALIZED versus REALIZED
    Unrealized P&L is a mark to liquidation (exit at bid - slippage for the long leg and ask + slippage for the
    short leg), net of the ACTUAL entry costs and ESTIMATED exit costs. It is only used to trigger exits and is
    labelled as such. Realized P&L comes only from the actual simulated exit fills and actual costs.
"""
from collections import namedtuple
from decimal import Decimal

from ..costs.models import BUY, SELL, LegExecution, paise
from ..errors import ContractError, CostRateUnverified
from ..execution import paper as X
from ..guards import paper_only
from ..market.ist import to_ist
from ..regime import engine as regime_engine
from ..risk import engine as risk_engine
from ..contracts import hhmm

OPEN, EXIT_PENDING, CLOSED, UNCLOSED_CRITICAL = "OPEN", "EXIT_PENDING", "CLOSED", "UNCLOSED_CRITICAL"
TERMINAL = (CLOSED, UNCLOSED_CRITICAL)
HOLD, CLOSE = "HOLD", "CLOSE"
FORCED_FLAT_BY, MANUAL_PAPER_EXIT, DATA_SAFETY, STOP_LOSS, REGIME_INVALIDATION, PROFIT_TARGET = (
    "FORCED_FLAT_BY", "MANUAL_PAPER_EXIT", "DATA_SAFETY", "STOP_LOSS", "REGIME_INVALIDATION", "PROFIT_TARGET")
SETUP_INVALIDATED = "SETUP_INVALIDATED"
PRIORITY = (FORCED_FLAT_BY, MANUAL_PAPER_EXIT, DATA_SAFETY, STOP_LOSS, REGIME_INVALIDATION, SETUP_INVALIDATED,
            PROFIT_TARGET)

ManagedPosition = namedtuple("ManagedPosition", (
    "position_id approval_id candidate_id strategy_type entry_regime lots state entry_time exit_reason exit_triggers "
    "forced exit_requested_at exit_attempts last_pending_reasons bad_data_streak closed_at critical history"))
PositionDecision = namedtuple("PositionDecision", (
    "action primary_reason triggers at unrealized bad_data_streak data_problems regime_label note"))
ManageResult = namedtuple("ManageResult", "position decision execution")
CriticalException = namedtuple("CriticalException", "position_id raised_at text")
Event = namedtuple("Event", "at from_state to_state note")

NOTE_UNREALIZED = ("UNREALIZED mark to liquidation: exit legs at bid - slippage / ask + slippage, minus ACTUAL entry costs and "
                   "ESTIMATED exit costs. Used only to trigger exits; it is not a realized result.")


def _event(at, a, b, note):
    return Event(at.isoformat(), a, b, note)


class PositionManager(object):
    def __init__(self, cfg, params, executor, risk_manager, provider, calendar, regime_params, cost_engine,
                 data_mode="fixture", regime_fn=None, data_label=None, setup_exit_fn=None):
        paper_only.assert_paper_only(cfg)
        self._setup_exit_fn = setup_exit_fn          # (now, entry regime label) -> reason text or None
        self._cfg, self._p, self._ex, self._risk = cfg, params, executor, risk_manager
        self._provider, self._calendar, self._rp, self._costs = provider, calendar, regime_params, cost_engine
        self._data_mode, self._data_label = data_mode, data_label
        self._regime_fn = regime_fn
        self._positions = {}
        self._criticals = []

    # ---- views
    def position(self, position_id):
        return self._positions.get(position_id)

    def positions(self):
        return tuple(self._positions.values())

    def active(self):
        return tuple(p for p in self._positions.values() if p.state not in TERMINAL)

    @property
    def critical_exceptions(self):
        return tuple(self._criticals)

    # ---- entry (HOLD starts here; only after a successful paper fill)
    def enter(self, approval, candidate, lots, now):
        """Execute the entry; returns (ExecutionResult, ManagedPosition or None). No averaging, no extra lots."""
        paper_only.assert_paper_only(self._cfg)
        now = to_ist(now)
        conflicts = self._entry_conflicts(candidate, lots)
        if conflicts:
            return self._ex.record_rejection(X.ENTRY, now, approval, candidate, conflicts), None
        result = self._ex.execute_entry(approval, candidate, lots, now)
        if result.status != X.FILLED:
            return result, None
        pos = ManagedPosition(
            result.execution_id, approval.approval_id, result.candidate_id, candidate.strategy_type,
            candidate.regime_at_entry["label"], lots, OPEN, now, None, (), False, None, 0, (), 0, None, None,
            (_event(now, None, OPEN, "entry filled"),))
        self._positions[pos.position_id] = pos
        return result, pos

    def _entry_conflicts(self, candidate, lots):
        """Defence in depth behind the risk engine: one position per contract pair, never more than the lot ceiling."""
        out = []
        mine = {candidate.long_contract.symbol, candidate.short_contract.symbol}
        open_lots = 0
        for p in self.active():
            cand = self._ex.position(p.position_id).candidate
            open_lots += p.lots
            if mine & {cand.long_contract.symbol, cand.short_contract.symbol}:
                out.append(("DUPLICATE_POSITION", "a position already holds %s: no adding, averaging or rolling" % (
                    ", ".join(sorted(mine & {cand.long_contract.symbol, cand.short_contract.symbol})))))
        if open_lots + lots > risk_engine.effective_max_lots(self._cfg):
            out.append(("POSITION_LOT_CEILING", "%d open + %d new lot(s) exceeds the ceiling" % (open_lots, lots)))
        return out

    # ---- evaluation
    def _regime(self, now):
        if self._regime_fn is not None:
            return self._regime_fn(now)
        return regime_engine.classify_at(self._provider, self._calendar, now, self._rp, self._data_mode,
                                          self._data_label)

    def _unrealized(self, pos, fills, now):
        epos = self._ex.position(pos.position_id)
        s = epos.candidate.spread
        entry = {f.leg_id: f for f in epos.entry_fills}
        exit_ = {f.leg_id: f for f in fills}
        gross = ((exit_["long"].fill_price - entry["long"].fill_price)
                 + (entry["short"].fill_price - exit_["short"].fill_price)) * s.units
        est = self._costs.exit_costs([LegExecution(f.leg_id, f.side, f.fill_price, f.units, now.date()) for f in fills])
        entry_costs = epos.actual_entry_costs.total
        return {"label": NOTE_UNREALIZED, "gross_pnl_inr": paise(gross), "actual_entry_costs_inr": paise(entry_costs),
                "estimated_exit_costs_inr": paise(est.total), "net_pnl_inr": paise(gross - entry_costs - est.total)}

    def evaluate(self, position_id, now, manual=False):
        """HOLD or CLOSE for one position at `now`. Reads quotes and the regime as known at `now`; records nothing."""
        now = to_ist(now)
        pos = self._positions[position_id]
        epos = self._ex.position(position_id)
        triggers, problems, unrealized, label = [], [], None, None
        if now.time() >= hhmm(self._cfg.flat_by_ist):
            triggers.append(FORCED_FLAT_BY)
        if manual:
            triggers.append(MANUAL_PAPER_EXIT)

        try:
            fills, quote_problems = self._ex.preview_exit(position_id, now)
        except ContractError as e:
            fills, quote_problems = (), [X.Reason("CONTRACT_UNAVAILABLE", str(e))]
        problems.extend(quote_problems)
        if not quote_problems:
            try:
                unrealized = self._unrealized(pos, fills, now)
            except (CostRateUnverified, ContractError) as e:
                problems.append(X.Reason("COSTS_UNAVAILABLE", str(e)))
        regime = self._regime(now)
        label = regime.label
        if label == regime_engine.NO_DATA:
            problems.append(X.Reason("REGIME_NO_DATA", "the regime has no usable underlying data"))
        streak = pos.bad_data_streak + 1 if problems else 0
        if streak >= self._p.data_safety_failed_checks:
            triggers.append(DATA_SAFETY)

        defined = self._ex.position(position_id).approval.defined_risk_inr
        if unrealized is not None:
            net = unrealized["net_pnl_inr"]
            if net <= -(defined * self._p.stop_loss_pct_of_defined_risk / 100):
                triggers.append(STOP_LOSS)
            if net >= defined * self._p.profit_target_pct_of_defined_risk / 100:
                triggers.append(PROFIT_TARGET)
        entry_label = pos.entry_regime
        if self._p.invalidate_on_opposite_regime and (
                (entry_label == regime_engine.BULLISH and label == regime_engine.BEARISH)
                or (entry_label == regime_engine.BEARISH and label == regime_engine.BULLISH)):
            triggers.append(REGIME_INVALIDATION)
        if self._p.invalidate_on_high_volatility and label == regime_engine.HIGH_VOLATILITY:
            triggers.append(REGIME_INVALIDATION)
        if self._setup_exit_fn is not None and self._setup_exit_fn(now, entry_label):
            triggers.append(SETUP_INVALIDATED)
        if pos.state == EXIT_PENDING and pos.exit_reason and pos.exit_reason not in triggers:
            triggers.append(pos.exit_reason)                       # a decided close stays decided
        ordered = tuple(t for t in PRIORITY if t in triggers)
        action = CLOSE if ordered else HOLD
        return PositionDecision(
            action, ordered[0] if ordered else None, ordered, now, unrealized, streak,
            tuple(problems), label, "HOLD: no exit trigger" if not ordered else "CLOSE: %s" % ordered[0])

    # ---- acting on a decision
    def manage(self, position_id, now, manual=False):
        """Evaluate one position and act: HOLD does nothing; CLOSE attempts the exit with fresh data."""
        paper_only.assert_paper_only(self._cfg)
        now = to_ist(now)
        pos = self._positions[position_id]
        if pos.state in TERMINAL:
            return ManageResult(pos, None, None)
        session = self._calendar.session(now.date())
        if session is None or now.time() >= session.close:
            self.end_of_session(now)
            return ManageResult(self._positions[position_id], None, None)
        d = self.evaluate(position_id, now, manual)
        pos = pos._replace(bad_data_streak=d.bad_data_streak)
        if d.action == HOLD:
            self._positions[position_id] = pos
            return ManageResult(pos, d, None)
        forced = pos.forced or FORCED_FLAT_BY in d.triggers
        if pos.exit_requested_at is None:
            pos = pos._replace(exit_requested_at=now.isoformat())
        pos = pos._replace(exit_reason=d.primary_reason, exit_triggers=d.triggers, forced=forced,
                           exit_attempts=pos.exit_attempts + 1)
        result = self._ex.execute_exit(position_id, now, d.primary_reason)
        if result.status == X.FILLED:
            pos = pos._replace(state=CLOSED, closed_at=now.isoformat(), last_pending_reasons=(),
                               history=pos.history + (_event(now, pos.state, CLOSED, "exit filled: %s" % d.primary_reason),))
        elif result.status == X.PENDING:
            reasons = tuple((r.code, r.text) for r in result.reasons)
            hist = pos.history
            if pos.state != EXIT_PENDING:
                hist = hist + (_event(now, pos.state, EXIT_PENDING, "%s: exit pending" % d.primary_reason),)
            pos = pos._replace(state=EXIT_PENDING, last_pending_reasons=reasons, history=hist)
        else:                                                      # REJECTED: nothing filled, stays where it is
            reasons = tuple((r.code, r.text) for r in result.reasons)
            pos = pos._replace(state=EXIT_PENDING, last_pending_reasons=reasons,
                               history=pos.history + (_event(now, pos.state, EXIT_PENDING, "exit rejected: %s" % (
                                   ", ".join(c for c, _ in reasons))),))
        self._positions[position_id] = pos
        return ManageResult(pos, d, result)

    def manage_all(self, now):
        return tuple(self.manage(p.position_id, now) for p in self.active())

    def close_manually(self, position_id, now):
        return self.manage(position_id, now, manual=True)

    def end_of_session(self, now):
        """At or after the session close any position still open becomes a CRITICAL exception, never 'closed'."""
        now = to_ist(now)
        session = self._calendar.session(now.date())
        if session is not None and now.time() < session.close:
            return ()
        raised = []
        for p in self.active():
            text = ("position %s was still %s when the session ended (%s). It is NOT closed; the risk ledger still "
                    "holds it open. Last pending reasons: %s" % (
                        p.position_id[:12], p.state, now.isoformat(),
                        "; ".join("%s" % c for c, _ in p.last_pending_reasons) or "none"))
            exc = CriticalException(p.position_id, now.isoformat(), text)
            self._criticals.append(exc)
            raised.append(exc)
            self._positions[p.position_id] = p._replace(
                state=UNCLOSED_CRITICAL, critical=text,
                history=p.history + (_event(now, p.state, UNCLOSED_CRITICAL, "session ended with the position open"),))
        return tuple(raised)

    # ---- P&L report
    def pnl_report(self, position_id, now=None):
        """Estimated, actual and unrealized figures in separate sections. Realized P&L only from actual fills."""
        pos = self._positions[position_id]
        epos = self._ex.position(position_id)
        def fills_json(fills):
            return [{"leg": f.leg_id, "contract": f.contract.symbol, "side": f.side, "units": f.units,
                     "bid": str(f.bid), "ask": str(f.ask), "reference_price": str(f.reference_price),
                     "slippage_points": str(f.slippage_points), "fill_price": str(f.fill_price)} for f in fills]
        entry_costs = epos.actual_entry_costs
        report = {
            "position_id": position_id, "state": pos.state, "exit_reason": pos.exit_reason, "paper": True,
            "estimated_at_approval": {
                "label": "ESTIMATE used only for the risk gate; not an actual result",
                "approved_total_defined_risk_inr": str(epos.approval.defined_risk_inr),
                "approved_capital_committed_inr": str(epos.approval.capital_committed_inr)},
            "actual_entry_fills": fills_json(epos.entry_fills),
            "actual_entry_costs_inr": str(paise(entry_costs.total)),
            "actual_exit_fills": fills_json(epos.exit_fills) if epos.exit_fills else None,
            "actual_exit_costs_inr": None if epos.actual_exit_costs is None else str(paise(epos.actual_exit_costs.total)),
            "gross_pnl_inr": None if epos.gross_pnl_inr is None else str(epos.gross_pnl_inr),
            "net_realized_pnl_inr": None if epos.net_pnl_inr is None else str(epos.net_pnl_inr),
            "unrealized": None,
        }
        if pos.state not in TERMINAL and now is not None:
            fills, problems = self._ex.preview_exit(position_id, to_ist(now))
            if not problems:
                u = self._unrealized(pos, fills, to_ist(now))
                report["unrealized"] = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in u.items()}
        return report
