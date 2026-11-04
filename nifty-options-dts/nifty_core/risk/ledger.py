"""The day's risk ledger: immutable, one IST trading day, with every quantity kept separate.

SIX DIFFERENT NUMBERS (never interchangeable)
    account_capital_inr         the virtual account size from configuration. It never changes inside the day.
    capital_committed_inr       cash tied up by OPEN positions: net debit paid (after entry slippage) plus entry
                                costs. It is released when a position closes.
    defined_risk_inr            for ONE trade: net debit + entry costs + estimated exit costs + slippage. This is
                                what the per-trade limit is tested against.
    open_risk_reserved_inr      the sum of defined_risk_inr over OPEN positions: the loss budget they still hold.
    realized_pnl_inr            net profit or loss (after actual costs) of positions CLOSED today.
    remaining_daily_risk_capacity_inr
                                max_daily_loss - realized loss used - open risk reserved (never below zero).
                                Realized PROFIT does not raise it: capital protection beats headroom.

Updates return a NEW ledger; nothing is mutated in place.
"""
import datetime
from collections import namedtuple
from decimal import Decimal

from ..errors import RiskViolation
from ..market.ist import to_ist

OPEN, CLOSED, CANCELLED = "OPEN", "CLOSED", "CANCELLED"
ZERO = Decimal(0)

Position = namedtuple("Position", (
    "approval_id candidate_id strategy_type long_symbol short_symbol lots defined_risk_inr capital_committed_inr "
    "opened_at state realized_pnl_inr closed_at"))

LedgerSnapshot = namedtuple("LedgerSnapshot", (
    "account_capital_inr capital_committed_inr open_risk_reserved_inr realized_pnl_inr realized_loss_used_inr "
    "remaining_daily_risk_capacity_inr trades_today open_positions open_lots"))


def snapshot_to_json(s):
    if s is None:
        return None
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in s._asdict().items()}


class DayLedger(object):
    __slots__ = ("day", "account_capital_inr", "positions")

    def __init__(self, day, account_capital_inr, positions=()):
        if not isinstance(day, datetime.date) or isinstance(day, datetime.datetime):
            raise ValueError("day must be a date")
        object.__setattr__(self, "day", day)
        object.__setattr__(self, "account_capital_inr", Decimal(account_capital_inr))
        object.__setattr__(self, "positions", tuple(positions))

    def __setattr__(self, name, value):
        raise AttributeError("DayLedger is immutable")

    # ---- views
    def open_positions(self):
        return tuple(p for p in self.positions if p.state == OPEN)

    def closed_positions(self):
        return tuple(p for p in self.positions if p.state == CLOSED)

    def candidate_ids(self):
        return frozenset(p.candidate_id for p in self.positions)

    def last_loss(self):
        """The most recently closed position that lost money, or None."""
        losses = [p for p in self.closed_positions() if p.realized_pnl_inr < 0]
        return max(losses, key=lambda p: p.closed_at) if losses else None

    def snapshot(self, max_daily_loss_inr):
        opened = self.open_positions()
        realized = sum((p.realized_pnl_inr for p in self.closed_positions()), ZERO)
        loss_used = max(ZERO, -realized)
        reserved = sum((p.defined_risk_inr for p in opened), ZERO)
        remaining = max(ZERO, Decimal(max_daily_loss_inr) - loss_used - reserved)
        counted = [p for p in self.positions if p.state != CANCELLED]     # a never-filled approval is not a trade
        return LedgerSnapshot(
            self.account_capital_inr, sum((p.capital_committed_inr for p in opened), ZERO), reserved, realized,
            loss_used, remaining, len(counted), len(opened), sum(p.lots for p in opened))

    # ---- updates
    def with_opened(self, approval, opened_at):
        if any(p.approval_id == approval.approval_id for p in self.positions):
            raise RiskViolation("APPROVAL_ALREADY_USED", "approval %s is already in the ledger" % approval.approval_id[:12])
        opened_at = to_ist(opened_at)
        if opened_at.date() != self.day:
            raise RiskViolation("LEDGER_DAY_MISMATCH", "ledger is for %s, not %s" % (self.day, opened_at.date()))
        pos = Position(approval.approval_id, approval.candidate_id, approval.strategy_type, approval.long_symbol,
                       approval.short_symbol, approval.lots, approval.defined_risk_inr,
                       approval.capital_committed_inr, opened_at, OPEN, None, None)
        return DayLedger(self.day, self.account_capital_inr, self.positions + (pos,))

    def with_cancelled(self, approval_id):
        """Release the reservation of an approval that never produced a fill. The candidate stays 'used'."""
        out, found = [], False
        for p in self.positions:
            if p.approval_id == approval_id:
                if p.state != OPEN:
                    raise RiskViolation("NOT_CANCELLABLE", "only an open reservation can be cancelled")
                p = p._replace(state=CANCELLED)
                found = True
            out.append(p)
        if not found:
            raise RiskViolation("UNKNOWN_POSITION", "no position for approval %s" % approval_id[:12])
        return DayLedger(self.day, self.account_capital_inr, out)

    def with_closed(self, approval_id, realized_pnl_inr, closed_at):
        closed_at = to_ist(closed_at)
        out, found = [], False
        for p in self.positions:
            if p.approval_id == approval_id:
                if p.state != OPEN:
                    raise RiskViolation("ALREADY_CLOSED", "position %s is already closed" % approval_id[:12])
                if closed_at < p.opened_at:
                    raise RiskViolation("CLOSE_BEFORE_OPEN", "a position cannot close before it opened")
                p = p._replace(state=CLOSED, realized_pnl_inr=Decimal(realized_pnl_inr), closed_at=closed_at)
                found = True
            out.append(p)
        if not found:
            raise RiskViolation("UNKNOWN_POSITION", "no position for approval %s" % approval_id[:12])
        return DayLedger(self.day, self.account_capital_inr, out)
