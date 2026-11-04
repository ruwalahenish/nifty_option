"""Paper execution: SIMULATED fills for an approved debit spread. Nothing here can reach a market.

There is no broker, no network, no credential and no order interface in this module. It reads quotes through the
read-only MarketDataProvider and records what a conservative simulation says would have happened.

FILL RULES (adverse slippage is cfg.slippage_points_per_leg, applied to every leg execution)
    ENTRY   long option BUY   at  ask + slippage
            short option SELL at  bid - slippage        (never below one tick)
    EXIT    long option SELL  at  bid - slippage        (never below one tick)
            short option BUY  at  ask + slippage
    The quotes are the ones known at the execution time. Nothing is ever filled at a mid price or an old price.

NO INVENTED FILLS
    A missing, future, stale, invalid, one-sided or off-tick quote, an expired contract, or a quoted size smaller than
    the order gives a PENDING result with the reasons. Both legs fill together or not at all: there are no partial
    fills and no single-leg positions.

APPROVAL AND INTEGRITY (entry)
    Accepts only a valid, unused P7 approval for the identical candidate and the approved lots. A tampered approval,
    a modified candidate, a changed quantity, a reused approval, an active kill switch, or an approval that is
    stale, from the future or outside the entry window gives REJECTED. A broken paper-only guard raises. Fills that
    would cost more than the approved Total Defined Risk are not made (PENDING, FILL_EXCEEDS_APPROVED_RISK).

ESTIMATED versus ACTUAL
    The P7 estimate (exit costs at the entry quotes) exists only to size the risk gate. It is recorded under
    "estimated_at_approval". Costs and P&L reported as "actual" come only from the simulated fill prices.

EXITS
    An exit needs no approval (it reduces risk), so it stays possible under the kill switch. It uses the same
    quote rules. Its net P&L (gross from both legs' fill prices, minus actual entry and exit costs) is passed to the
    risk ledger.
"""
import hashlib
import datetime
from collections import namedtuple
from decimal import Decimal

from ..costs.engine import QuotedLeg, slipped_price
from ..costs.models import BUY, SELL, LegExecution, paise
from ..data import validation as data_validation
from ..errors import (ContractError, CostRateUnverified, DataNotAvailable, KillSwitchActive, RiskViolation)
from ..guards import paper_only
from ..market.ist import to_ist
from ..risk import engine as risk_engine
from ..contracts import hhmm

FILLED, PENDING, REJECTED = "FILLED", "PENDING", "REJECTED"
ENTRY, EXIT = "ENTRY", "EXIT"
OPEN, CLOSED = "OPEN", "CLOSED"
MODEL_VERSION = 1
NOTE = ("SIMULATED paper execution. No order was sent anywhere. Fills are modelled from quotes with adverse "
        "slippage and assume the quoted size is available; real fills would differ.")

Reason = namedtuple("Reason", "code text")
LegFill = namedtuple("LegFill", (
    "leg_id contract side lots units bid ask quote_time quote_age_seconds reference_price slippage_points fill_price"))
ExecutionResult = namedtuple("ExecutionResult", (
    "status kind execution_id approval_id candidate_id decision_time approved_at executed_at reasons legs actual "
    "estimated_at_approval fingerprints note"))
PaperPosition = namedtuple("PaperPosition", (
    "execution_id approval candidate candidate_id lots entry_time entry_fills actual_entry_costs state exit_time "
    "exit_fills actual_exit_costs gross_pnl_inr net_pnl_inr"))


def _id(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _costs_json(phase):
    return {"label": phase.label, "total_inr": str(phase.total),
            "components": {k: str(v) for k, v in phase.components.items()},
            "per_leg": {l.execution.leg_id: {"price": str(l.execution.price), "total_inr": str(l.total)}
                        for l in phase.legs}}


def result_to_journal(r):
    return {
        "status": r.status, "kind": r.kind, "execution_id": r.execution_id, "approval_id": r.approval_id,
        "candidate_id": r.candidate_id,
        "decision_time": None if r.decision_time is None else r.decision_time.isoformat(),
        "approved_at": None if r.approved_at is None else r.approved_at.isoformat(),
        "executed_at": r.executed_at.isoformat(),
        "reasons": [{"code": x.code, "text": x.text} for x in r.reasons],
        "legs": [{"leg": f.leg_id, "contract": f.contract.symbol, "side": f.side, "lots": f.lots, "units": f.units,
                  "bid": str(f.bid), "ask": str(f.ask), "quote_time": f.quote_time.isoformat(),
                  "quote_age_seconds": f.quote_age_seconds, "reference_price": str(f.reference_price),
                  "slippage_points": str(f.slippage_points), "fill_price": str(f.fill_price)} for f in r.legs],
        "actual": r.actual, "estimated_at_approval": r.estimated_at_approval, "fingerprints": r.fingerprints,
        "paper": True, "note": r.note,
    }


class PaperExecutor(object):
    def __init__(self, cfg, cost_engine, specs, calendar, risk_manager, provider):
        paper_only.assert_paper_only(cfg)
        self._cfg, self._costs, self._specs, self._calendar = cfg, cost_engine, specs, calendar
        self._risk, self._provider = risk_manager, provider
        self._positions = {}
        self._log = []

    # ---- read-only views
    @property
    def log(self):
        """Every attempt, filled or not, in order."""
        return tuple(self._log)

    def position(self, execution_id):
        return self._positions.get(execution_id)

    def open_positions(self):
        return tuple(p for p in self._positions.values() if p.state == OPEN)

    # ---- helpers
    def _fingerprints(self, candidate):
        a = candidate.assumptions
        return {"config": risk_engine.config_fingerprint(self._cfg), "candidate": risk_engine.candidate_fingerprint(candidate),
                "strategy_thresholds": a.get("strategy_thresholds", {}).get("fingerprint"),
                "regime_thresholds": a.get("regime_thresholds_fingerprint"), "execution_model": MODEL_VERSION,
                "slippage_points_per_leg": str(self._cfg.slippage_points_per_leg)}

    def _estimate(self, candidate, day):
        calc = risk_engine.calculate(candidate, self._cfg, self._costs, day)
        keep = ("net_debit_quoted_inr", "entry_slippage_inr", "entry_costs_inr", "estimated_exit_costs_inr",
                "total_defined_risk_inr", "exit_estimate_method")
        out = {k: str(calc[k]) for k in keep}
        out["label"] = ("ESTIMATE made for the risk gate (exit costs assume both legs close at their entry quotes). "
                        "It is not an actual result.")
        return out

    def _quote_problems(self, contract, side, units, now, reference_side_is_ask):
        """(quote or None, [Reason]) for one leg. side is the leg's trade side (BUY pays the ask, SELL hits the bid)."""
        try:
            q = self._provider.get_quote(contract, as_of=now)
        except DataNotAvailable as e:
            return None, [Reason("MISSING_QUOTE", "%s: %s" % (contract.symbol, e))]
        out, sym = [], contract.symbol
        if q.timestamp > now:
            out.append(Reason("QUOTE_FROM_FUTURE", "%s quote is stamped %s, after %s" % (sym, q.timestamp, now)))
        else:
            age = (now - q.timestamp).total_seconds()
            if age > self._cfg.data_stale_after_seconds:
                out.append(Reason("STALE_QUOTE", "%s quote is %d s old (limit %d s)" % (
                    sym, age, self._cfg.data_stale_after_seconds)))
        if not q.is_two_sided:
            out.append(Reason("ONE_SIDED_QUOTE", "%s needs a bid and an ask above zero (bid=%s ask=%s)" % (
                sym, q.bid, q.ask)))
        else:
            try:
                tick = Decimal(str(self._specs.tick_size(now.date(), self._cfg.data_mode).value))
                if q.bid % tick != 0 or q.ask % tick != 0:
                    out.append(Reason("OFF_TICK_QUOTE", "%s bid/ask is not a multiple of the %s tick" % (sym, tick)))
            except ContractError as e:
                out.append(Reason("TICK_UNVERIFIED", str(e)))
            qty = q.ask_qty if reference_side_is_ask else q.bid_qty
            if qty is None:
                out.append(Reason("MISSING_QUOTED_SIZE", "%s quoted size on the %s side is not reported" % (
                    sym, "ask" if reference_side_is_ask else "bid")))
            elif qty < units:
                out.append(Reason("INSUFFICIENT_QUOTED_SIZE", "%s quoted size %d is below the %d units to trade "
                                  "(no partial fills are simulated)" % (sym, qty, units)))
        try:
            if data_validation.contract_expired(contract, now, self._calendar):
                out.append(Reason("EXPIRED_CONTRACT", "%s has expired" % sym))
        except ContractError as e:
            out.append(Reason("CALENDAR_UNAVAILABLE", str(e)))
        return q, out

    def _price_legs(self, legs, now):
        """legs: [(leg_id, contract, side, lots, units)]. Returns (fills, [Reason]); fills only if no reason."""
        problems, quotes = [], {}
        for leg_id, contract, side, lots, units in legs:
            q, probs = self._quote_problems(contract, side, units, now, reference_side_is_ask=(side == BUY))
            problems.extend(probs)
            quotes[leg_id] = q
        if problems:
            return (), problems
        tick = Decimal(str(self._specs.tick_size(now.date(), self._cfg.data_mode).value))
        fills = []
        for leg_id, contract, side, lots, units in legs:
            q = quotes[leg_id]
            ref = q.ask if side == BUY else q.bid
            fill = slipped_price(ref, side, self._cfg.slippage_points_per_leg, tick)
            fills.append(LegFill(leg_id, contract, side, lots, units, q.bid, q.ask, q.timestamp,
                                 int((now - q.timestamp).total_seconds()), ref, self._cfg.slippage_points_per_leg,
                                 fill))
        return tuple(fills), []

    def _finish(self, status, kind, now, approval, candidate, reasons, fills=(), actual=None, estimated=None, eid=None):
        r = ExecutionResult(
            status, kind, eid, getattr(approval, "approval_id", None),
            risk_engine.candidate_fingerprint(candidate) if hasattr(candidate, "spread") else None,
            getattr(candidate, "decision_time", None), getattr(approval, "approved_at", None), now, tuple(reasons),
            tuple(fills), actual or {}, estimated or {}, self._fingerprints(candidate) if hasattr(candidate, "spread")
            else {}, NOTE)
        self._log.append(r)
        return r

    # ---- entry
    def execute_entry(self, approval, candidate, lots, now):
        """Simulate the entry of an approved spread at `now`. Returns an ExecutionResult (never places anything)."""
        paper_only.assert_paper_only(self._cfg)
        now = to_ist(now)
        fin = lambda status, reasons, **kw: self._finish(status, ENTRY, now, approval, candidate, reasons, **kw)

        # 1 integrity: the approval must be the genuine, unused one for this exact candidate and quantity
        try:
            self._risk.verify_entry(approval, candidate, lots)
        except KillSwitchActive as e:
            return fin(REJECTED, [Reason("KILL_SWITCH_ACTIVE", str(e))])
        except RiskViolation as e:
            return fin(REJECTED, [Reason(e.code, str(e))])

        # 2 the approval must still be fresh and inside the entry window
        problems = []
        if now < approval.approved_at:
            problems.append(Reason("APPROVAL_FROM_FUTURE", "approved at %s, after now (%s)" % (
                approval.approved_at.isoformat(), now.isoformat())))
        elif (now - approval.approved_at).total_seconds() > self._cfg.data_stale_after_seconds:
            problems.append(Reason("APPROVAL_STALE", "approval is %d s old (limit %d s)" % (
                (now - approval.approved_at).total_seconds(), self._cfg.data_stale_after_seconds)))
        if candidate.decision_time.date() != now.date():
            problems.append(Reason("CANDIDATE_OTHER_DAY", "the candidate is not from today"))
        if now.time() < hhmm(self._cfg.entry_window_start_ist) or now.time() > hhmm(self._cfg.entry_window_end_ist):
            problems.append(Reason("OUTSIDE_ENTRY_WINDOW", "%s is outside the entry window" % now.strftime("%H:%M:%S")))
        if now.time() >= hhmm(self._cfg.flat_by_ist):
            problems.append(Reason("AFTER_FLAT_BY", "no entry at or after %s" % self._cfg.flat_by_ist))
        if problems:
            if any(p.code in ("APPROVAL_STALE", "AFTER_FLAT_BY", "OUTSIDE_ENTRY_WINDOW", "CANDIDATE_OTHER_DAY")
                   for p in problems):
                self._risk.cancel_unused(approval)               # never filled: release the reserved risk
            return fin(REJECTED, problems)

        # 3 quotes for both legs, or nothing
        s = candidate.spread
        legs = [("long", s.long_leg.contract, BUY, s.lots, s.units), ("short", s.short_leg.contract, SELL, s.lots, s.units)]
        fills, problems = self._price_legs(legs, now)
        if problems:
            return fin(PENDING, problems)
        day = now.date()
        try:
            estimated = self._estimate(candidate, day)
            tick = Decimal(str(self._specs.tick_size(day, self._cfg.data_mode).value))
            # the fills must stay inside the approved Total Defined Risk (fresh quotes may have moved against us)
            fresh = self._costs.defined_risk(
                [QuotedLeg("long", BUY, fills[0].reference_price, s.units),
                 QuotedLeg("short", SELL, fills[1].reference_price, s.units)],
                self._cfg.slippage_points_per_leg, day, tick)
            actual_entry = self._costs.actual_costs([
                LegExecution(f.leg_id, f.side, f.fill_price, f.units, day) for f in fills])
        except CostRateUnverified as e:
            return fin(PENDING, [Reason("COST_RATES_UNVERIFIED", str(e))])
        except ContractError as e:
            return fin(PENDING, [Reason("COSTS_UNAVAILABLE", str(e))])
        if fresh.total_defined_risk_inr > approval.defined_risk_inr:
            return fin(PENDING, [Reason(
                "FILL_EXCEEDS_APPROVED_RISK", "at the current quotes the defined risk is %s, above the approved %s: "
                "no fill is made" % (fresh.total_defined_risk_inr, approval.defined_risk_inr))],
                estimated=estimated)

        # 4 commit: use the approval up, record the position
        self._risk.confirm_entry(approval, candidate, lots)
        eid = _id("ENTRY", approval.approval_id, now.isoformat())
        paid = sum(((f.fill_price if f.side == BUY else -f.fill_price) * f.units for f in fills), Decimal(0))
        actual = {"label": "ACTUAL simulated fills and costs", "net_debit_paid_inr": str(paise(paid)),
                  "entry_costs": _costs_json(actual_entry),
                  "entry_cash_outlay_inr": str(paise(paid + actual_entry.total)),
                  "defined_risk_at_fill_inr": str(fresh.total_defined_risk_inr)}
        pos = PaperPosition(eid, approval, candidate, risk_engine.candidate_fingerprint(candidate), lots, now, fills,
                            actual_entry, OPEN, None, (), None, None, None)
        self._positions[eid] = pos
        return fin(FILLED, [], fills=fills, actual=actual, estimated=estimated, eid=eid)

    def preview_exit(self, execution_id, now):
        """What an exit at `now` would fill at, WITHOUT recording anything: (fills, problems). Used to mark positions."""
        pos = self._positions.get(execution_id)
        if pos is None or pos.state != OPEN:
            return (), [Reason("NOT_OPEN", "no open paper position %s" % str(execution_id)[:12])]
        s = pos.candidate.spread
        legs = [("long", s.long_leg.contract, SELL, s.lots, s.units), ("short", s.short_leg.contract, BUY, s.lots, s.units)]
        return self._price_legs(legs, to_ist(now))

    def record_rejection(self, kind, now, approval, candidate, reasons):
        """Log a refusal made by a layer above (for example the position manager)."""
        return self._finish(REJECTED, kind, to_ist(now), approval, candidate, [Reason(c, t) for c, t in reasons])

    # ---- exit
    def execute_exit(self, execution_id, now, reason="MANUAL"):
        """Simulate closing both legs of an open paper position at `now` and pass the actual net P&L to the risk ledger."""
        paper_only.assert_paper_only(self._cfg)
        now = to_ist(now)
        pos = self._positions.get(execution_id)
        if pos is None:
            r = ExecutionResult(REJECTED, EXIT, execution_id, None, None, None, None, now,
                                (Reason("UNKNOWN_POSITION", "no paper position %s" % str(execution_id)[:12]),), (), {},
                                {}, {}, NOTE)
            self._log.append(r)
            return r
        cand, appr = pos.candidate, pos.approval
        fin = lambda status, reasons, **kw: self._finish(status, EXIT, now, appr, cand, reasons, eid=execution_id, **kw)
        if pos.state != OPEN:
            return fin(REJECTED, [Reason("ALREADY_CLOSED", "position %s is already closed" % execution_id[:12])])
        if now < pos.entry_time:
            return fin(REJECTED, [Reason("EXIT_BEFORE_ENTRY", "an exit cannot precede the entry")])
        if now.date() != pos.entry_time.date():
            return fin(REJECTED, [Reason("EXIT_ON_ANOTHER_DAY", "positions are intraday only; this one should have "
                                         "been closed on %s" % pos.entry_time.date())])
        s = cand.spread
        legs = [("long", s.long_leg.contract, SELL, s.lots, s.units), ("short", s.short_leg.contract, BUY, s.lots, s.units)]
        fills, problems = self._price_legs(legs, now)
        if problems:
            return fin(PENDING, problems)
        day = now.date()
        try:
            actual_exit = self._costs.actual_costs([LegExecution(f.leg_id, f.side, f.fill_price, f.units, day) for f in fills])
        except (CostRateUnverified, ContractError) as e:
            return fin(PENDING, [Reason("COSTS_UNAVAILABLE", str(e))])
        entry_by_leg = {f.leg_id: f for f in pos.entry_fills}
        exit_by_leg = {f.leg_id: f for f in fills}
        gross = ((exit_by_leg["long"].fill_price - entry_by_leg["long"].fill_price)
                 + (entry_by_leg["short"].fill_price - exit_by_leg["short"].fill_price)) * s.units
        gross = paise(gross)
        entry_costs = paise(pos.actual_entry_costs.total)
        net = paise(gross - entry_costs - actual_exit.total)
        closed = pos._replace(state=CLOSED, exit_time=now, exit_fills=fills, actual_exit_costs=actual_exit,
                              gross_pnl_inr=gross, net_pnl_inr=net)
        self._positions[execution_id] = closed
        self._risk.record_close(appr, net, now)
        actual = {"label": "ACTUAL simulated fills and costs", "exit_reason": reason,
                  "gross_pnl_inr": str(gross), "actual_entry_costs_inr": str(entry_costs),
                  "actual_exit_costs": _costs_json(actual_exit), "net_pnl_inr": str(net),
                  "formula": "net = gross P&L from both legs' fill prices - actual entry costs - actual exit costs"}
        return fin(FILLED, [], fills=fills, actual=actual, estimated=self._estimate(cand, pos.entry_time.date()))
