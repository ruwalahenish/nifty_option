"""Version 1 risk engine: an independent APPROVED / BLOCKED decision on a strategy candidate.

IT NEVER ADJUSTS ANYTHING
    It does not change a strike, a premium, a width, a quantity or a strategy assumption to make a trade fit.
    A candidate either passes every check as it is, or it is BLOCKED with every reason recorded.

TOTAL DEFINED RISK (the per-trade figure)
    net debit quoted + entry slippage            = net debit paid
    + entry costs (brokerage, STT, exchange charge, IPFT, SEBI fee, stamp duty, GST, leg by leg)
    + estimated exit costs (both legs closed at their entry quotes: the conservative estimate)
    = total defined risk, compared with the per-trade limit (risk_per_trade_inr). Equal passes; one paisa
    above blocks. There is no stop-loss input anywhere: a stop-loss can never reduce this figure.

CHECKS (all are run; every failure is recorded)
    paper-only guard (raises), kill switch, candidate integrity (consistent, immutable, not from the future,
    not stale, same day), entry window and flat-by time, no overnight position, no expired contract, lot size
    agrees with the contract layer, no uncovered short, lots <= min(max_lots, HARD_MAX_LOTS) counting open
    positions, trades today < max_trades_per_day, per-trade defined risk, daily loss limit, remaining daily risk
    capacity (open risk reserved counts), capital committed <= account capital, no averaging (no overlap with an
    open position), no re-use of a candidate, no recovery trade that was not decided after the loss.

APPROVAL
    An approval carries the candidate's SHA-256 fingerprint, the approved lots and its own digest. RiskManager
    .confirm_entry() re-checks all of it before anything may be entered, once, and refuses a changed candidate or
    a changed quantity.
"""
import datetime
import hashlib
import json
from collections import namedtuple
from decimal import Decimal

from ..contracts import hhmm
from ..data import validation as data_validation
from ..errors import (ContractError, CostRateUnverified, KillSwitchActive, RiskViolation)
from ..guards import paper_only
from ..instruments.coverage import uncovered_shorts
from ..instruments.spread import KIND_BEAR_PUT, KIND_BULL_CALL, DebitSpread
from ..market.ist import to_ist
from ..costs.models import paise
from ..strategy import engine as strategy_engine
from . import ledger as ledger_mod

APPROVED, BLOCKED = "APPROVED", "BLOCKED"
HARD_MAX_LOTS = 1            # Version 1 ceiling. Configuration can lower the limit, never raise it.
NOTE = ("Defined risk is the worst case under the stated cost and slippage assumptions: net debit + entry costs + "
        "estimated exit costs + slippage. It is not a forecast, and a stop-loss does not reduce it. APPROVED means "
        "only that no limit was breached; it is not a recommendation.")

Reason = namedtuple("Reason", "code text")
Approval = namedtuple("Approval", (
    "approval_id candidate_id strategy_type long_symbol short_symbol lots defined_risk_inr capital_committed_inr "
    "approved_at config_fingerprint digest"))
RiskDecision = namedtuple("RiskDecision", (
    "outcome candidate_id now reasons calculation limits ledger_before ledger_after trades_before trades_after "
    "fingerprints approval note"))


# ---- identity and integrity ------------------------------------------------------------------------------------
def candidate_fingerprint(candidate):
    """SHA-256 over the candidate's complete journal record. Any change to any field changes it."""
    blob = json.dumps(strategy_engine.candidate_to_journal(candidate), sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def config_fingerprint(cfg):
    keys = ("account_capital_inr", "risk_per_trade_pct", "max_daily_loss_pct", "max_lots", "max_trades_per_day",
            "risk_basis", "slippage_points_per_leg", "entry_window_start_ist", "entry_window_end_ist", "flat_by_ist",
            "allow_averaging", "allow_overnight", "data_mode", "data_stale_after_seconds", "paper")
    blob = json.dumps({k: str(getattr(cfg, k)) for k in keys}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _digest(parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _approval_parts(a):
    return (a.approval_id, a.candidate_id, a.strategy_type, a.long_symbol, a.short_symbol, a.lots,
            a.defined_risk_inr, a.capital_committed_inr, a.approved_at.isoformat(), a.config_fingerprint)


def approval_is_authentic(approval):
    """True if the approval's fields still match the digest made when it was issued."""
    return isinstance(approval, Approval) and approval.digest == _digest(_approval_parts(approval))


def effective_max_lots(cfg):
    return min(cfg.max_lots, HARD_MAX_LOTS)


def _consistency(c):
    """Problems (code, text) if the candidate's summary fields disagree with its own validated spread."""
    s = getattr(c, "spread", None)
    if not isinstance(s, DebitSpread):
        return [("CANDIDATE_INVALID", "the candidate carries no validated DebitSpread")]
    out = []

    def check(ok, what):
        if not ok:
            out.append(("CANDIDATE_INCONSISTENT", what))
    check(c.long_contract == s.long_leg.contract and c.short_contract == s.short_leg.contract,
          "contracts differ from the spread's legs")
    check(c.lots == s.lots and c.lot_size == s.lot_size, "lots or lot size differ from the spread")
    check(c.entry_price_long == s.long_leg.entry_price and c.entry_price_short == s.short_leg.entry_price,
          "entry prices differ from the spread's legs")
    check(c.net_debit_points == s.net_debit_points and c.width_points == s.width_points,
          "net debit or width differ from the spread")
    check(c.net_debit_inr == s.net_debit_points * s.units, "net debit in rupees differs from the spread")
    check(c.strategy_type == s.kind and c.strategy_type in (KIND_BULL_CALL, KIND_BEAR_PUT),
          "strategy type differs from the spread")
    return out


def _structure(c):
    """Independent re-check of the shape: one long, one covered short, same type/expiry/units."""
    s = c.spread
    long_leg, short_leg = s.long_leg, s.short_leg
    out = []
    if not long_leg.is_long or short_leg.is_long:
        out.append(("STRUCTURE_INVALID", "need exactly one long and one short leg"))
    if (long_leg.underlying, long_leg.expiry, long_leg.option_type, long_leg.units) != \
            (short_leg.underlying, short_leg.expiry, short_leg.option_type, short_leg.units):
        out.append(("STRUCTURE_INVALID", "legs must share underlying, expiry, option type and quantity"))
    for leg, units in uncovered_shorts([long_leg, short_leg]):
        out.append(("NAKED_SHORT", "short %s would be uncovered for %d units" % (leg.contract.symbol, units)))
    return out


# ---- calculation -----------------------------------------------------------------------------------------------
def calculate(candidate, cfg, cost_engine, day):
    """The defined-risk figures as a dict of Decimals (and text), or raises a cost/contract error."""
    dr = candidate.spread.defined_risk(cost_engine, cfg.slippage_points_per_leg, trade_date=day)
    return {
        "formula": "total_defined_risk = net_debit_quoted + entry_slippage + entry_costs + estimated_exit_costs",
        "net_debit_quoted_inr": dr.net_debit_quoted_inr,
        "slippage_points_per_leg": cfg.slippage_points_per_leg,
        "entry_slippage_inr": dr.entry_slippage_inr,
        "net_debit_paid_inr": dr.net_debit_paid_inr,
        "entry_costs_inr": dr.entry_costs.total,
        "entry_cost_components": dict(dr.entry_costs.components),
        "estimated_exit_costs_inr": dr.estimated_exit_costs.total,
        "exit_cost_components": dict(dr.estimated_exit_costs.components),
        "exit_estimate_method": dr.exit_estimate_method,
        "round_trip_costs_inr": dr.round_trip_costs_inr,
        "entry_risk_inr": dr.entry_risk_inr,
        "total_defined_risk_inr": dr.total_defined_risk_inr,
        "stop_loss_used": False,
        "warnings": list(dr.warnings),
    }


def _calc_json(calc):
    if calc is None:
        return None
    out = {}
    for k, v in calc.items():
        if isinstance(v, dict):
            out[k] = {kk: str(vv) for kk, vv in v.items()}
        elif isinstance(v, Decimal):
            out[k] = str(v)
        else:
            out[k] = v
    return out


# ---- the decision ----------------------------------------------------------------------------------------------
def evaluate(candidate, ledger, now, cfg, cost_engine, specs, calendar, kill_dir):
    """Decide APPROVED or BLOCKED for `candidate` at `now`, given the day's `ledger`. Pure: nothing is recorded.

    Raises LiveTradingNotSupported if paper mode is not intact (that is a hard refusal, not a decision).
    """
    paper_only.assert_paper_only(cfg)
    if kill_dir is None:
        raise ValueError("kill_dir is required: the kill switch cannot be skipped")
    now = to_ist(now)
    day = now.date()
    reasons = []

    def block(code, text):
        reasons.append(Reason(code, text))

    cid = candidate_fingerprint(candidate) if hasattr(candidate, "spread") else "invalid"
    limits = {
        "account_capital_inr": str(cfg.account_capital_inr),
        "risk_per_trade_inr": str(cfg.risk_per_trade_inr), "max_daily_loss_inr": str(cfg.max_daily_loss_inr),
        "max_trades_per_day": cfg.max_trades_per_day, "max_lots_configured": cfg.max_lots,
        "max_lots_effective": effective_max_lots(cfg), "hard_max_lots": HARD_MAX_LOTS,
        "entry_window_ist": [cfg.entry_window_start_ist, cfg.entry_window_end_ist], "flat_by_ist": cfg.flat_by_ist,
        "quote_stale_after_seconds": cfg.data_stale_after_seconds, "allow_averaging": False, "allow_overnight": False}
    before = ledger.snapshot(cfg.max_daily_loss_inr)
    fps = {"config": config_fingerprint(cfg), "candidate": cid}

    def finish(calc):
        return RiskDecision(BLOCKED, cid, now, tuple(reasons), calc, limits, before, None, before.trades_today, None,
                            fps, None, NOTE)

    if kill_switch_active(kill_dir):
        block("KILL_SWITCH_ACTIVE", "the kill switch file is present: no new entry is allowed")

    problems = _consistency(candidate)
    for code, text in problems:
        block(code, text)
    if problems and problems[0][0] == "CANDIDATE_INVALID":
        return finish(None)
    for code, text in _structure(candidate):
        block(code, text)
    fps["strategy_thresholds"] = candidate.assumptions.get("strategy_thresholds", {}).get("fingerprint")
    fps["regime_thresholds"] = candidate.assumptions.get("regime_thresholds_fingerprint")

    # time, day, overnight
    if candidate.decision_time > now:
        block("CANDIDATE_FROM_FUTURE", "the candidate was decided at %s, after now (%s)" % (
            candidate.decision_time.isoformat(), now.isoformat()))
    elif (now - candidate.decision_time).total_seconds() > cfg.data_stale_after_seconds:
        block("CANDIDATE_STALE", "the candidate is %d s old (limit %d s)" % (
            (now - candidate.decision_time).total_seconds(), cfg.data_stale_after_seconds))
    if candidate.decision_time.date() != day:
        block("CANDIDATE_OTHER_DAY", "the candidate was decided on %s, not today (%s)" % (
            candidate.decision_time.date(), day))
    if ledger.day != day:
        block("LEDGER_DAY_MISMATCH", "the ledger is for %s, not %s" % (ledger.day, day))
    if any(p.opened_at.date() != day for p in ledger.open_positions()):
        block("OVERNIGHT_POSITION_PRESENT", "a position from an earlier day is still open")
    if now.time() < hhmm(cfg.entry_window_start_ist) or now.time() > hhmm(cfg.entry_window_end_ist):
        block("OUTSIDE_ENTRY_WINDOW", "%s is outside the entry window %s-%s" % (
            now.strftime("%H:%M:%S"), cfg.entry_window_start_ist, cfg.entry_window_end_ist))
    if now.time() >= hhmm(cfg.flat_by_ist):
        block("AFTER_FLAT_BY", "%s is at or after the flat-by time %s: no overnight positions" % (
            now.strftime("%H:%M:%S"), cfg.flat_by_ist))

    # data origin, contract layer, expiry
    if candidate.data_mode != cfg.data_mode:
        block("DATA_MODE_MISMATCH", "candidate data_mode %r differs from configuration %r" % (
            candidate.data_mode, cfg.data_mode))
    if cfg.data_mode != "fixture" and candidate.data_label == "FIXTURE_SYNTHETIC":
        block("DATA_UNVERIFIED", "synthetic data cannot be approved when data_mode=%r" % cfg.data_mode)
    spread = candidate.spread
    try:
        layer_lot = specs.lot_size(spread.expiry, cfg.data_mode).value
        if layer_lot != spread.lot_size:
            block("LOT_SIZE_MISMATCH", "candidate lot size %d differs from the contract layer's %d" % (
                spread.lot_size, layer_lot))
    except ContractError as e:
        block("LOT_SIZE_UNVERIFIED", str(e))
    try:
        for leg in (spread.long_leg, spread.short_leg):
            if data_validation.contract_expired(leg.contract, now, calendar):
                block("EXPIRED_CONTRACT", "%s cannot be opened at %s" % (leg.contract.symbol, now.isoformat()))
    except ContractError as e:
        block("CALENDAR_UNAVAILABLE", str(e))

    # defined risk
    calc = None
    try:
        calc = calculate(candidate, cfg, cost_engine, day)
    except CostRateUnverified as e:
        block("COST_RATES_UNVERIFIED", str(e))
    except ContractError as e:
        block("COSTS_UNAVAILABLE", str(e))
    risk = calc["total_defined_risk_inr"] if calc else None
    committed = calc["entry_risk_inr"] if calc else None

    # lots, trades, exposure
    max_lots = effective_max_lots(cfg)
    if candidate.lots > max_lots:
        block("MAX_LOTS_EXCEEDED", "%d lot(s) requested, limit %d per position" % (candidate.lots, max_lots))
    if before.open_lots + candidate.lots > max_lots:
        block("MAX_LOTS_EXCEEDED_COMBINED", "%d open + %d new lot(s) exceeds the limit of %d" % (
            before.open_lots, candidate.lots, max_lots))
    if before.trades_today >= cfg.max_trades_per_day:
        block("MAX_TRADES_REACHED", "%d trade(s) already taken today, limit %d" % (
            before.trades_today, cfg.max_trades_per_day))
    if risk is not None:
        if risk > cfg.risk_per_trade_inr:
            block("PER_TRADE_RISK_EXCEEDED", "total defined risk %s exceeds the per-trade limit %s by %s" % (
                risk, cfg.risk_per_trade_inr, risk - cfg.risk_per_trade_inr))
        if before.realized_loss_used_inr >= cfg.max_daily_loss_inr:
            block("DAILY_LOSS_LIMIT_REACHED", "realized loss today %s has reached the daily limit %s" % (
                before.realized_loss_used_inr, cfg.max_daily_loss_inr))
        if risk > before.remaining_daily_risk_capacity_inr:
            block("DAILY_RISK_CAPACITY_EXCEEDED", "defined risk %s exceeds the remaining daily capacity %s "
                  "(limit %s - realized loss %s - open risk reserved %s)" % (
                      risk, before.remaining_daily_risk_capacity_inr, cfg.max_daily_loss_inr,
                      before.realized_loss_used_inr, before.open_risk_reserved_inr))
        if before.capital_committed_inr + committed > cfg.account_capital_inr:
            block("CAPITAL_EXCEEDED", "capital committed %s + %s exceeds account capital %s" % (
                before.capital_committed_inr, committed, cfg.account_capital_inr))

    # no bypass: averaging, re-use, splitting, recovery trades
    mine = {spread.long_leg.contract.symbol, spread.short_leg.contract.symbol}
    for p in ledger.open_positions():
        if mine & {p.long_symbol, p.short_symbol}:
            block("AVERAGING_BLOCKED", "an open position already holds %s: adding to it is averaging" % (
                ", ".join(sorted(mine & {p.long_symbol, p.short_symbol}))))
    if cid in ledger.candidate_ids():
        block("CANDIDATE_ALREADY_USED", "this exact candidate was already approved today: no splitting or repeating")
    last_loss = ledger.last_loss()
    if last_loss is not None and candidate.decision_time <= last_loss.closed_at:
        block("RECOVERY_TRADE_NOT_INDEPENDENT", "a loss closed at %s; a new trade needs a strategy decision made after "
              "it (this one is from %s)" % (last_loss.closed_at.isoformat(), candidate.decision_time.isoformat()))
    if last_loss is not None and candidate.lots > last_loss.lots:
        block("RECOVERY_SIZE_INCREASE", "after a loss, lots may not increase (%d > %d)" % (
            candidate.lots, last_loss.lots))

    if reasons:
        return finish(calc)

    approval_id = _digest((cid, now.isoformat(), fps["config"], before.trades_today))
    parts = Approval(approval_id, cid, candidate.strategy_type, spread.long_leg.contract.symbol,
                     spread.short_leg.contract.symbol, candidate.lots, risk, committed, now, fps["config"], "")
    approval = parts._replace(digest=_digest(_approval_parts(parts)))
    after = ledger.with_opened(approval, now).snapshot(cfg.max_daily_loss_inr)
    return RiskDecision(APPROVED, cid, now, (), calc, limits, before, after, before.trades_today, after.trades_today,
                        fps, approval, NOTE)


def kill_switch_active(kill_dir):
    return paper_only.kill_switch_active(kill_dir)


def decision_to_journal(d):
    return {
        "outcome": d.outcome, "candidate_id": d.candidate_id, "decided_at": d.now.isoformat(),
        "reasons": [{"code": r.code, "text": r.text} for r in d.reasons],
        "calculation": _calc_json(d.calculation), "limits": d.limits,
        "ledger_before": ledger_mod.snapshot_to_json(d.ledger_before),
        "ledger_after": ledger_mod.snapshot_to_json(d.ledger_after),
        "trades_before": d.trades_before, "trades_after": d.trades_after, "fingerprints": d.fingerprints,
        "approval": None if d.approval is None else {
            "approval_id": d.approval.approval_id, "candidate_id": d.approval.candidate_id, "lots": d.approval.lots,
            "defined_risk_inr": str(d.approval.defined_risk_inr),
            "capital_committed_inr": str(d.approval.capital_committed_inr), "digest": d.approval.digest},
        "note": d.note,
    }


# ---- stateful wrapper ------------------------------------------------------------------------------------------
class RiskManager(object):
    """Holds the day's ledger. request_entry() decides and, only if APPROVED, reserves the risk atomically, so an
    approval cannot be obtained twice for the same exposure. confirm_entry() is what any later entry step must
    call: it refuses a modified candidate, a changed quantity, a reused approval or an active kill switch."""

    def __init__(self, cfg, cost_engine, specs, calendar, kill_dir, day):
        paper_only.assert_paper_only(cfg)
        if kill_dir is None:
            raise ValueError("kill_dir is required")
        self._cfg, self._costs, self._specs, self._calendar, self._kill = cfg, cost_engine, specs, calendar, kill_dir
        self._ledger = ledger_mod.DayLedger(day, cfg.account_capital_inr)
        self._confirmed = set()

    @property
    def ledger(self):
        return self._ledger

    def snapshot(self):
        return self._ledger.snapshot(self._cfg.max_daily_loss_inr)

    def carry_over(self, positions):
        """Add trades that already happened earlier today in a previous run of the program (ledger Position tuples,
        already closed), so the day's trade count, realised loss and used-candidate checks continue instead of
        starting again from zero."""
        self._ledger = ledger_mod.DayLedger(self._ledger.day, self._ledger.account_capital_inr,
                                            self._ledger.positions + tuple(positions))
        return self.snapshot()

    def request_entry(self, candidate, now):
        d = evaluate(candidate, self._ledger, now, self._cfg, self._costs, self._specs, self._calendar, self._kill)
        if d.outcome == APPROVED:
            self._ledger = self._ledger.with_opened(d.approval, d.now)
        return d

    def verify_entry(self, approval, candidate, lots):
        """Everything confirm_entry checks, but WITHOUT using the approval up. Raises like confirm_entry."""
        paper_only.assert_paper_only(self._cfg)
        paper_only.assert_no_kill(self._kill)
        if not approval_is_authentic(approval):
            raise RiskViolation("APPROVAL_TAMPERED", "the approval no longer matches its digest")
        if candidate_fingerprint(candidate) != approval.candidate_id:
            raise RiskViolation("CANDIDATE_MODIFIED", "the candidate differs from the one that was approved")
        if lots != approval.lots:
            raise RiskViolation("QUANTITY_CHANGED", "approved for %d lot(s), asked for %r" % (approval.lots, lots))
        if not any(p.approval_id == approval.approval_id and p.state == ledger_mod.OPEN
                   for p in self._ledger.positions):
            raise RiskViolation("APPROVAL_NOT_OPEN", "no open reserved position for this approval")
        if approval.approval_id in self._confirmed:
            raise RiskViolation("APPROVAL_ALREADY_USED", "this approval has already been used once")
        return True

    def confirm_entry(self, approval, candidate, lots):
        """Raise RiskViolation / KillSwitchActive / LiveTradingNotSupported unless this exact approved entry may go
        ahead; on success the approval is used up (it works once)."""
        self.verify_entry(approval, candidate, lots)
        self._confirmed.add(approval.approval_id)
        return True

    def cancel_unused(self, approval):
        """Release the reserved risk of a genuine approval that was never used (for example, it went stale)."""
        if not approval_is_authentic(approval):
            raise RiskViolation("APPROVAL_TAMPERED", "the approval no longer matches its digest")
        if approval.approval_id in self._confirmed:
            raise RiskViolation("APPROVAL_ALREADY_USED", "a used approval cannot be cancelled")
        self._ledger = self._ledger.with_cancelled(approval.approval_id)
        return self.snapshot()

    def record_close(self, approval, realized_pnl_inr, closed_at):
        self._ledger = self._ledger.with_closed(approval.approval_id, realized_pnl_inr, closed_at)
        return self.snapshot()
